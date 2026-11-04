"""The pilot behind RFC-047 section 2: what the query translator does today.

Three questions, asked of the product's own `MarianQueryTranslator` and of
`langdetect` with the product's seed, on the *validation* queries of
RFC-042's frozen set (the test split is not read):

1. Detection: which language `langdetect` assigns to the Portuguese, the
   English and the southern queries, and to RFC-023's single words.
2. English through the translator: what Marian does to a query that is
   already English, which decides whether "translate everything that is not
   English" is safe.
3. Substitution: whether replacing a word the translator gets wrong with a
   plainer Portuguese word, before translating, fixes the English. Only the
   failures RFC-023 and RFC-042 already reported are tried, in sentences
   written for this pilot, not the evaluation queries.

    .venv\\Scripts\\python.exe experiments\\rfc-047-traducao-das-consultas\\pilot.py

Writes `pilot.json` next to this script.
"""

from __future__ import annotations

import io
import json
import re
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[1]
sys.path[:0] = [str(REPOSITORY / "backend")]

from langdetect import detect, detect_langs  # noqa: E402

from app.infrastructure.ai.device import resolve_device  # noqa: E402
from app.infrastructure.ai.model_runtime import (  # noqa: E402
    TRANSLATOR,
    default_runtime,
)
from app.infrastructure.ai.query_translator import MarianQueryTranslator  # noqa: E402

EVAL_SET = REPOSITORY / "experiments" / "rfc-042-fine-tuning" / "eval_set.jsonl"
ROMANCE = frozenset({"pt", "es", "it", "fr", "ro", "ca"})
"""The `langdetect` codes of the languages `opus-mt-ROMANCE-en` translates from."""

RFC_023_WORDS = ("fazenda", "lago", "piscina", "farm")

# Single-word queries, the case `langdetect` handles worst (RFC-023 section
# 7.1): common things in aerial photos, written for this pilot in both
# languages, pair by pair.
SHORT = (
    ("fazenda", "farm"),
    ("lago", "lake"),
    ("piscina", "pool"),
    ("rio", "river"),
    ("praia", "beach"),
    ("cidade", "city"),
    ("igreja", "church"),
    ("ponte", "bridge"),
    ("estrada", "road"),
    ("floresta", "forest"),
    ("morro", "hill"),
    ("campo", "field"),
    ("casa", "house"),
    ("prédio", "building"),
    ("estádio", "stadium"),
    ("porto", "harbor"),
    ("ilha", "island"),
    ("cachoeira", "waterfall"),
    ("plantação", "plantation"),
    ("represa", "reservoir"),
    ("galpão", "shed"),
    ("estacionamento", "parking lot"),
    ("rua", "street"),
    ("avenida", "avenue"),
    ("mar", "sea"),
    ("montanha", "mountain"),
    ("deserto", "desert"),
    ("neve", "snow"),
    ("escola", "school"),
    ("usina", "power plant"),
)

# (as typed, with the troublesome word replaced by a plainer one). The words
# are the failures RFC-023 section 22 and RFC-042 section 14 reported; the
# sentences are new.
SUBSTITUTIONS = (
    ("casarão antigo perto da estrada", "casa grande antiga perto da estrada"),
    ("casarão amarelo com jardim", "casa grande amarela com jardim"),
    ("terreno com mato alto", "terreno com vegetação alta"),
    ("casa no meio do mato", "casa no meio da vegetação"),
    ("rio com corredeiras e pedras", "rio com águas agitadas e pedras"),
    ("trapiche de madeira no lago", "píer de madeira no lago"),
    ("lagoa de água barrenta", "lagoa de água marrom"),
    ("rio barrento perto da ponte", "rio marrom perto da ponte"),
    ("chácara com um galpão à esquerda", "sítio com um galpão à esquerda"),
    ("chácara com piscina", "pequena fazenda com piscina"),
)


def plain(text: str) -> str:
    """Casefolded, without accents or punctuation: to compare English outputs."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    letters = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", letters))


def top_language(text: str) -> tuple[str, float]:
    try:
        best = detect_langs(text)[0]
    except Exception:  # noqa: BLE001 -- langdetect's own "no features" error
        return "?", 0.0
    return best.lang, round(best.prob, 3)


def main() -> None:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    rows = [
        json.loads(line)
        for line in EVAL_SET.read_text(encoding="utf-8").splitlines()
        if line
    ]
    validation = [row for row in rows if row["split"] == "validation"]
    groups = {
        "pt": [row["pt"] for row in validation],
        "en": [row["en"] for row in validation],
        "sul": [row["pt_south"] for row in validation if "pt_south" in row],
    }
    report: dict[str, Any] = {
        "queries": {name: len(texts) for name, texts in groups.items()}
    }

    print("1. Detection, validation queries")
    detection: dict[str, Any] = {}
    for name, texts in groups.items():
        labels = [detect(text) for text in texts]
        counts = Counter(labels)
        expected = "en" if name == "en" else "pt"
        misses = [
            {"text": text, "detected": label, "top": top_language(text)}
            for text, label in zip(texts, labels, strict=True)
            if label != expected
        ]
        romance = sum(label in ROMANCE for label in labels)
        detection[name] = {"labels": dict(counts), "misses": misses, "romance": romance}
        print(f"  {name}: {dict(counts.most_common())}; Romance {romance}/{len(texts)}")
        for miss in misses:
            print(f"     {miss['detected']} {miss['top']}: {miss['text']}")
    words = {word: top_language(word) for word in RFC_023_WORDS}
    detection["rfc_023_words"] = words
    print(f"  RFC-023 words: {words}")
    report["detection"] = detection

    translator = MarianQueryTranslator(
        default_runtime().loader(TRANSLATOR), device=resolve_device()
    )
    translator._translate("aquecimento")  # load the checkpoint before timing

    print("\n2. English queries through Marian")
    started = time.perf_counter()
    outputs = [translator._translate(text) for text in groups["en"]]
    seconds = time.perf_counter() - started
    changed = [
        {"in": text, "out": out}
        for text, out in zip(groups["en"], outputs, strict=True)
        if plain(text) != plain(out)
    ]
    report["english_through_marian"] = {
        "queries": len(outputs),
        "changed": len(changed),
        "seconds_per_query": seconds / len(outputs),
        "changes": changed,
    }
    milliseconds = 1000 * seconds / len(outputs)
    print(f"  {len(changed)} of {len(outputs)} change; {milliseconds:.0f} ms per query")
    for change in changed:
        print(f"     {change['in']}  ->  {change['out']}")

    print("\n3. Substitution before translating")
    pairs = []
    for original, replaced in SUBSTITUTIONS:
        pair = {
            "original": original,
            "original_en": translator._translate(original),
            "replaced": replaced,
            "replaced_en": translator._translate(replaced),
        }
        pairs.append(pair)
        print(f"  {original}  ->  {pair['original_en']}")
        print(f"  {replaced}  ->  {pair['replaced_en']}\n")
    report["substitution"] = pairs

    print("4. Single-word queries")
    short: list[dict[str, Any]] = []
    for portuguese, english in SHORT:
        entry: dict[str, Any] = {
            "pt": portuguese,
            "pt_detected": top_language(portuguese),
            "pt_marian": translator._translate(portuguese),
            "en": english,
            "en_detected": top_language(english),
            "en_marian": translator._translate(english),
        }
        short.append(entry)
        print(
            f"  {portuguese:15} {entry['pt_detected']!s:16} -> {entry['pt_marian']:18}"
            f" | {english:12} {entry['en_detected']!s:16} -> {entry['en_marian']}"
        )
    pt_missed = sum(e["pt_detected"][0] != "pt" for e in short)
    en_missed = sum(e["en_detected"][0] != "en" for e in short)
    en_damaged = sum(
        e["en_detected"][0] != "en" and plain(e["en_marian"]) != plain(e["en"])
        for e in short
    )
    report["short"] = {
        "pairs": short,
        "pt_not_detected": pt_missed,
        "en_not_detected": en_missed,
        "en_not_detected_and_changed_by_marian": en_damaged,
    }
    print(f"  pt not detected as pt: {pt_missed}/{len(short)};", end="")
    print(f" en not detected as en: {en_missed}/{len(short)},", end="")
    print(f" of which Marian changes {en_damaged}")

    (HERE / "pilot.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"wrote {HERE / 'pilot.json'}")


if __name__ == "__main__":
    main()
