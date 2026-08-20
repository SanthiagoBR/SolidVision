"""Portuguese-to-English query normalization for the CLIP text encoder.

CLIP's text tower is English-only. Feeding it a Portuguese query directly
measurably degrades retrieval: in the RFC-023 bake-off the selected
checkpoint scored 44.0% strict accuracy on raw Portuguese against 52.0%
once the same queries were machine-translated to English first
(`clip_pt_translation_run_output.log`). Translation therefore happens
here, in Infrastructure, so that neither `EmbeddingModelPort` nor any
Application code has to learn that the current model speaks one language.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import torch
from langdetect import DetectorFactory, LangDetectException, detect
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer, PreTrainedModel
from transformers.tokenization_utils_base import PreTrainedTokenizerBase

from app.infrastructure.ai.device import resolve_device
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

# `langdetect`'s algorithm is probabilistic and seeded per process. Pinning
# the seed is what makes `detect()` reproducible across runs, processes, and
# machines -- the same determinism requirement that rules out the salted
# built-in `hash()` in `FakeEmbeddingModel` (ARCHITECTURE.md section 20).
# Set at import time because `detect()` reads it lazily on first use.
DetectorFactory.seed = 0

TRANSLATION_MODEL = "Helsinki-NLP/opus-mt-ROMANCE-en"
PORTUGUESE = "pt"
# opus-mt-ROMANCE-en is many-to-one over the Romance languages, so the source
# language is selected by a leading target token rather than by the checkpoint.
PORTUGUESE_TOKEN = ">>por<<"
MAX_TRANSLATION_TOKENS = 128


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
    keeps the `>>por<<` source token and the decoding parameters visible
    instead of buried in pipeline defaults.

    Decoding is greedy (`num_beams=1`, `do_sample=False`) so the same query
    always yields the same English string, and therefore the same embedding.

    Known limitation, deliberately not solved here: `langdetect` needs a few
    words to work. Full-sentence queries classify correctly, but one- or
    two-word queries are unreliable -- `'fazenda'` is detected as Turkish and
    `'lago'` as Tagalog, so neither is translated and both reach CLIP in
    Portuguese. Building a better detector is out of scope for RFC-023
    (section 8); anything not confidently Portuguese is treated as English.
    """

    def __init__(
        self,
        model_name: str = TRANSLATION_MODEL,
        device: torch.device | None = None,
    ) -> None:
        self._model_name = model_name
        self._device = device if device is not None else resolve_device()
        self._tokenizer: PreTrainedTokenizerBase | None = None
        self._model: PreTrainedModel | None = None

    def to_english(self, text: str) -> str:
        """Translate `text` when it is Portuguese, otherwise return it unchanged."""
        if not self._is_portuguese(text):
            return text

        translated = self._translate(text)
        logger.debug("Translated query %r -> %r", text, translated)
        return translated

    @staticmethod
    def _is_portuguese(text: str) -> bool:
        try:
            return bool(detect(text) == PORTUGUESE)
        except LangDetectException:
            # Raised for input with no detectable features -- empty strings,
            # pure punctuation, bare digits. There is nothing to translate,
            # so hand it to CLIP untouched rather than failing the query.
            return False

    def _translate(self, text: str) -> str:
        tokenizer, model = self._ensure_loaded()

        batch = tokenizer(
            [f"{PORTUGUESE_TOKEN} {text}"],
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
        entire fast test suite -- never pays for a second model download or
        the memory it occupies (RFC-023 section 10).
        """
        if self._tokenizer is None or self._model is None:
            logger.info(
                "Loading translation model %s onto %s", self._model_name, self._device
            )
            model = AutoModelForSeq2SeqLM.from_pretrained(self._model_name)
            model.eval()
            self._model = model.to(self._device)
            self._tokenizer = AutoTokenizer.from_pretrained(self._model_name)

        return self._tokenizer, self._model
