"""Tradução das consultas do português para o inglês, para o encoder de texto do CLIP.

A torre de texto do CLIP só entende inglês. Entregar a ela uma consulta em
português, direto, piora a recuperação de forma mensurável: no bake-off do
RFC-023, o checkpoint escolhido fez 44,0% de acurácia estrita no português
cru, contra 52,0% quando as mesmas consultas eram antes traduzidas para o
inglês por máquina (`clip_pt_translation_run_output.log`). Por isso a
tradução acontece aqui, na Infrastructure, para que nem `EmbeddingModelPort`
nem nenhum código da Application precise saber que o modelo atual fala uma
só língua.

Desde o RFC-047, duas coisas acontecem antes do tradutor, ambas guiadas pelo
léxico ao lado deste arquivo, `query_lexicon.toml`: uma consulta é traduzida
quando o `langdetect` a diz português *ou* quando ela tem um marcador de
português (§6), e as formas do léxico que o tradutor erra são trocadas, antes
que ele as veja, por palavras que ele acerta (§5). Nenhuma das duas toca uma
consulta que não é traduzida, e nenhuma muda vetor guardado: o vetor da
consulta é calculado a cada busca.
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

# O algoritmo do `langdetect` é probabilístico e recebe uma semente por
# processo. Fixar a semente é o que torna `detect()` reproduzível entre
# execuções, processos e máquinas -- a mesma exigência de determinismo que
# descarta o `hash()` embutido, com sal, em `FakeEmbeddingModel`
# (ARCHITECTURE.md §20). Definida no import porque `detect()` a lê sob
# demanda, no primeiro uso.
DetectorFactory.seed = 0

# `TRANSLATION_MODEL` ficava aqui, o nome pelo qual o checkpoint era carregado.
# Desde o RFC-035, o tradutor é a entrada `translator` do lockfile, carregada
# do diretório dela; o nome está em `models.lock.json`.
PORTUGUESE = "pt"
MAX_TRANSLATION_TOKENS = 128

LEXICON_NAME = "query_lexicon.toml"
"""Ao lado deste módulo. RFC-047 §4: versionado com o código, não em
`Settings`. Ele muda o vetor de algumas consultas, o que é decisão de um RFC,
como o template do CLIP; ao contrário do template, não muda vetor guardado."""

ORIGINS = frozenset({"geral", "sul"})
"""De onde vem uma forma: o vocabulário geral, ou o sul do Brasil."""

FUNCTION_WORDS = frozenset(
    {
        "de", "da", "das", "dos", "com", "um", "uma", "uns", "umas",
        "em", "num", "numa", "na", "nas", "ao", "aos", "à", "às",
        "pelo", "pela", "pelos", "pelas", "por", "para", "entre", "perto",
        "sobre", "sem", "e", "ou", "nao", "não",
    }
)  # fmt: skip
"""RFC-047 §6: a lista fechada de palavras funcionais do português.

Nenhuma delas é também uma palavra do inglês -- "a", "as", "do", "no" e "on"
ficam de fora por isso --, então uma delas numa consulta basta para dizê-la
português. Comparadas como digitadas, ignorando maiúsculas mas não acentos:
"à" está na lista e "a" não."""

_WORD = re.compile("[\\w\u0300-\u036f]+")
"""Uma palavra: letras e dígitos, com qualquer acento combinante dentro dela."""

_JOINER = re.compile("[\\s\\-'\u2019]+")
"""O que pode ficar entre as palavras de uma forma de várias palavras
("estrada de chão", "cata-vento", "caixa d'água"): espaços, hífens,
apóstrofos, e nenhuma outra pontuação."""


def fold(text: str) -> str:
    """Sem maiúsculas e sem acentos: como as formas do léxico são comparadas.

    "Chacara", "chácara" e "CHÁCARA" viram todas "chacara".
    """
    decomposed = unicodedata.normalize("NFD", text.casefold())
    bare = "".join(c for c in decomposed if not unicodedata.combining(c))
    return unicodedata.normalize("NFC", bare)


@dataclass(frozen=True)
class LexiconForm:
    """Uma linha do léxico: uma forma em português, como digitada, e o inglês dela.

    `en` lista o que conta como tradução certa; os testes lentos conferem que
    o que vai ao Marian produz uma delas. `troca` só existe quando o Marian
    erra a forma, e é o que vai ao Marian no lugar dela. Uma forma com
    `tambem_ingles` é também uma palavra do inglês ("hotel"), e nunca é
    marcador de português.
    """

    pt: str
    en: tuple[str, ...]
    origem: str
    troca: str | None = None
    tambem_ingles: bool = False

    @property
    def sent(self) -> str:
        """O que chega ao Marian no lugar da forma (§5, regra 3)."""
        return self.troca if self.troca is not None else self.pt

    @property
    def words(self) -> tuple[str, ...]:
        return tuple(_WORD.findall(fold(self.pt)))

    @property
    def is_name(self) -> bool:
        """Uma forma escrita com maiúscula é nome de lugar ("Mato Grosso").

        Está no léxico só para que a correspondência mais longa a pegue
        inteira, antes que uma forma dentro dela seja trocada (RFC-047 §11,
        risco 2).
        """
        return self.pt[0].isupper()


class LexiconError(ValueError):
    """O arquivo do léxico está malformado. A mensagem diz a linha."""


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
    """As formas de `query_lexicon.toml`, achadas numa consulta (RFC-047 §5).

    Uma correspondência é de palavras inteiras, ignorando maiúsculas e
    acentos; a forma mais longa que começa numa palavra ganha, então "estrada
    de chão" é pega inteira antes que "chão" sozinho pudesse ser. A busca
    corre da esquerda para a direita e nunca se sobrepõe.
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
        """O léxico em `path` ou, por padrão, o desta versão.

        O padrão é lido como dado deste pacote, por `importlib.resources`,
        como `model_runtime` lê o lockfile: o arquivo viaja com o código, e
        só `layout` pode saber onde o código está (RFC-034).
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
        """`text` como vai ao Marian: cada forma trocada pelo que ela envia.

        Tudo fora das formas achadas -- pontuação, palavras que o léxico não
        conhece -- fica exatamente como digitado. Uma forma digitada com
        maiúscula a mantém: o Marian lê "Porto Alegre" como nome e "porto
        Alegre" como *port Joy* (RFC-047 §15).
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
        """Se `text` tem um marcador de português (RFC-047 §6, regra 2): uma
        palavra funcional da lista fechada, ou uma forma que não é também inglês.
        """
        if any(word.casefold() in FUNCTION_WORDS for word in _WORD.findall(text)):
            return True
        return any(not match.form.tambem_ingles for match in self.matches(text))


LEXICON = QueryLexicon.load()
"""Carregado uma vez, no import (RFC-047 §13)."""


def _detected_as_portuguese(text: str) -> bool:
    try:
        return bool(detect(text) == PORTUGUESE)
    except LangDetectException:
        # Levantada para entradas sem traço detectável -- strings vazias,
        # só pontuação, só dígitos. Não há o que traduzir, então a entrada
        # vai intocada para o CLIP, em vez de a consulta falhar.
        return False


def is_portuguese(text: str, lexicon: QueryLexicon = LEXICON) -> bool:
    """RFC-047 §6: um marcador, ou o `langdetect` dizendo `pt`.

    Os marcadores são conferidos primeiro porque são baratos, e porque
    decidem os casos que o `langdetect` erra: palavras soltas que ele diz
    turco ou tagalo, frases que ele diz espanhol. Ele ainda decide o resto:
    frases inteiras em português sem marcador.
    """
    return lexicon.has_marker(text) or _detected_as_portuguese(text)


def marian_input(text: str, lexicon: QueryLexicon = LEXICON) -> str | None:
    """O que o Marian recebe para esta consulta, ou None quando não é traduzida."""
    if not is_portuguese(text, lexicon):
        return None
    return lexicon.rewrite(text)


class QueryTranslator(ABC):
    """Normaliza uma consulta para o inglês que o encoder de texto do CLIP espera."""

    @abstractmethod
    def to_english(self, text: str) -> str:
        """Devolve `text` em inglês, traduzindo só quando é preciso."""


class MarianQueryTranslator(QueryTranslator):
    """Detecta português e o traduz com `Helsinki-NLP/opus-mt-ROMANCE-en`.

    Carregado por `AutoTokenizer` / `AutoModelForSeq2SeqLM`, e não por
    `pipeline("translation")`: este checkpoint não está registrado de forma
    confiável sob essa tarefa genérica no transformers 5.15, e o par
    explícito também deixa os parâmetros de decodificação à vista, em vez de
    enterrados nos defaults do pipeline.

    Nenhum token de idioma `>>xx<<` é enviado na frente: este checkpoint é
    muitos-para-um (toda língua românica para o inglês), e modelos Marian
    muitos-para-um não têm esses tokens no vocabulário. Acrescentar um mesmo
    assim -- p. ex. `>>por<<` -- resolve para `<unk>`, o que tira a
    decodificação gulosa da distribuição; conferido neste checkpoint, isso
    fazia o decoder entrar em laço em "." até `MAX_TRANSLATION_TOKENS` o
    cortar, em vez de produzir uma tradução.

    A decodificação é gulosa (`num_beams=1`, `do_sample=False`), para que a
    mesma consulta dê sempre a mesma string em inglês, e portanto o mesmo
    embedding.

    Os pesos são os do `main` fixado, cuja única cópia no Hub é
    `pytorch_model.bin`, um pickle. `vendor_models.py` o converte num
    `model.safetensors` -- os mesmos tensores, conferidos um a um, e 2,2 s
    mais rápido de carregar a frio --, então isto nunca desserializa pickle
    nenhum (RFC-035 §7.4). Não o `safetensors` do `refs/pr/6` do Hub: esse é
    o pull request de um bot, que pode mudar ou sumir.

    Quais consultas são traduzidas, e o que o Marian recebe, é do RFC-047:
    `marian_input()`. O `langdetect` precisa de algumas palavras para
    funcionar -- `'fazenda'` é detectada como turco e `'lago'` como tagalo --,
    então uma consulta também é traduzida quando tem um marcador de português,
    e uma forma do léxico que o Marian erra ("morro" -> *Death*) chega a ele
    como uma palavra que ele acerta. Uma palavra só, fora do léxico, continua
    a lacuna conhecida do RFC-023: o que não for marcado nem detectado como
    português é tratado como inglês.
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
        """O do produto, `query_lexicon.toml`; outro só nos testes."""
        self._tokenizer: PreTrainedTokenizerBase | None = None
        self._model: PreTrainedModel | None = None
        self._load_lock = threading.Lock()
        """Uma carga por vez, como faz `ClipEmbeddingModel` (RFC-036 5.3)."""

    def to_english(self, text: str) -> str:
        """Traduz `text` quando é português; senão, o devolve sem mudança.

        Uma consulta que não é traduzida também nunca chega ao léxico: vai ao
        CLIP exatamente como foi digitada, como antes do RFC-047.
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
            # `PreTrainedModel` não declara `generate` no transformers 5.15,
            # então o mypy o resolve por `nn.Module.__getattr__` e acusa o
            # `Tensor` resultante como não chamável. Uma lacuna de tipagem da
            # biblioteca, não um erro real; `warn_unused_ignores` vai apontar
            # esta linha se uma versão futura anotar o método.
            generated = model.generate(  # type: ignore[operator]
                **inputs,
                max_new_tokens=MAX_TRANSLATION_TOKENS,
                num_beams=1,
                do_sample=False,
            )

        decoded: str = tokenizer.batch_decode(generated, skip_special_tokens=True)[0]
        return decoded.strip()

    def _ensure_loaded(self) -> tuple[PreTrainedTokenizerBase, PreTrainedModel]:
        """Carrega o checkpoint de tradução uma vez, na primeira consulta em português.

        Sob demanda, e não antecipado, para que uma instalação só em inglês
        -- e a suíte rápida de testes inteira -- nunca pague a carga de um
        segundo modelo nem a memória que ele ocupa (RFC-023 §10). O diretório
        é conferido antes, e `local_files_only` é escrito nas duas chamadas,
        pelos motivos que `ClipEmbeddingModel._ensure_loaded()` dá -- e a
        carga acontece uma vez, sob um lock, pelo motivo que ele também dá: a
        primeira busca em português pode chegar enquanto o warm-up carrega
        isto.
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
