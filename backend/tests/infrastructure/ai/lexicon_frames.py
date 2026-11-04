"""The contexts a lexicon form is held to, and how an answer is judged.

RFC-047 section 17.

Shared by `test_query_lexicon_slow.py` and the experiment's
`probe_frames.py`, so that what the probe chose is what the test checks.

The first round proved each `troca` alone and in "{form} vista de cima",
and missed what it did to ordinary sentences: "estrada de terra" sent as
"estrada de terra batida" came back *dirt road hit near the river*. A form is
now held to `CONTEXTS`: the form alone, in the neutral sentence, and in
`FRAMES`, five sentences written for this and not taken from any evaluation
query (section 4.4). Each is translated by the product's own path --
`lexicon.rewrite(frame)` and then Marian -- and an answer is right when:

1. it contains one of the form's `en`, as whole words; and
2. it has no other word than those the frame explains (`FRAME_WORDS`), the
   matched `en` and the function words of `STOP`.

The second is what a word added by a `troca` ("batida" -> *hit*) or a word of
another sense ("sítio" -> *site*) breaks, and a bare "contains an `en`" does
not see: *dirt road hit near the river* contains *dirt road*.
"""

from __future__ import annotations

import re
import unicodedata

FRAMES = (
    "{X} perto do rio",
    "{X} com árvores dos dois lados",
    "vista de cima de {X} num dia nublado",
    "foto de {X} ao lado de uma casa branca",
    "{X} no meio de um campo verde",
)
"""No article before {X}: Portuguese gender is not known from the form."""

_FROM_ABOVE = frozenset(
    {"upper", "up", "over", "sight", "oversight", "overhead", "high", "bird", "eye"}
)

FRAME_WORDS: dict[str, frozenset[str]] = {
    "sozinha": frozenset(),
    "vista de cima": frozenset({"from"}) | _FROM_ABOVE,
    "frase 1": frozenset({"river"}),
    "frase 2": frozenset({"tree", "side"}),
    "frase 3": frozenset({"cloudy", "overcast", "day"}) | _FROM_ABOVE,
    "frase 4": frozenset({"white", "house", "side"}),
    "frase 5": frozenset({"middle", "green", "field", "countryside", "land"}),
}
"""What each frame says in English, by hand: a frame translated with a neutral
noun in {X} was not stable ("casa com árvores" came back *tree house*)."""

CONTEXTS: dict[str, str] = {
    "sozinha": "{X}",
    "vista de cima": "{X} vista de cima",
    **{f"frase {i}": frame for i, frame in enumerate(FRAMES, start=1)},
}

STOP = frozenset(
    "a an the of in on at to by with from and or next near beside along both "
    "side over under between into onto view above top seen aerial photo "
    "picture overlooking overview s".split()
)
"""English function words a frame's translation may rearrange freely."""

OTHER_SENSES: dict[str, tuple[tuple[str, str], ...]] = {
    "sítio": (("sítio arqueológico", "site"), ("sítio histórico", "site")),
}
"""A form with another everyday sense, and phrases of that sense with the
English word they must keep. "sítio" is a small farm in the south and a *site*
elsewhere; a `troca` toward the farm must not turn an archaeological site into
a farm."""


def plain(text: str) -> str:
    """Casefolded, without accents or punctuation."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    letters = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", letters))


def stem(word: str) -> str:
    """The word without its plural: "waves" and "wave" are the same here, as
    they are to CLIP, and a frame often turns a noun into a modifier."""
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def words(text: str) -> list[str]:
    return [stem(word) for word in plain(text).split()]


def found_all(english: str, accepted: tuple[str, ...]) -> list[str]:
    """Every one of `accepted` that `english` contains as whole words.

    An `en` of several words matches when all its words are there, in any
    order: Marian rearranges a frame around a noun ("foto de rua íngreme" is
    *street photo steep*), and the words are still the ones asked for.
    """
    present = set(words(english))
    return [w for w in accepted if set(words(w)) <= present]


def matched(english: str, accepted: tuple[str, ...]) -> str | None:
    """The longest of `accepted` that `english` contains as whole words."""
    return max(found_all(english, accepted), key=lambda w: len(words(w)), default=None)


def extras(english: str, context: str, accepted: tuple[str, ...]) -> list[str]:
    """Words of `english` that the frame, the `en` it matched and `STOP` do not
    explain: the words of every `en` it contains count, so that *crop* beside
    *field* is not an extra for a form that accepts both."""
    allowed = {stem(w) for w in STOP | FRAME_WORDS[context]}
    for entry in found_all(english, accepted):
        allowed |= set(words(entry))
    return [word for word in words(english) if word not in allowed]


def judge(
    english: str, context: str, accepted: tuple[str, ...]
) -> tuple[bool, str | None, list[str]]:
    """(right, the `en` found, the extra words) for one translated context."""
    found = matched(english, accepted)
    left = extras(english, context, accepted)
    return found is not None and not left, found, left
