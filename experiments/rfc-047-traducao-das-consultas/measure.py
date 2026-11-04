"""The measurement of RFC-047 section 9, and the `TBM`s around it.

RFC-042's `measure_baseline.py --only` is run twice on the validation split,
with the translator of before this RFC and with this RFC's; this reads both
results and checks the criteria section 9 fixed before measuring:

1. no English validation query changes its prompt (178 of 178);
2. among the Portuguese queries whose prompt changed, more move up than
   down; the ones whose prompt did not change keep their rank, query by query;
3. Portuguese recall@1 and recall@5 do not fall (the bootstrap interval is
   recorded, not required above zero);
4. of the pilot's 30 single words, the Portuguese ones in the lexicon reach
   CLIP with one of their `en`, and none of the English ones changes prompt;
5. the lexicon and the markers cost under 1 ms per query.

Also measured, without a criterion: the southern subset (section 4.4 says
why it is a ceiling), the gap left between Portuguese and English, the
reference queries of RFC-035 the lexicon touches (section 8), and risks 2
and 4 of section 11 on queries written here.

    .venv\\Scripts\\python.exe ^
        experiments\\rfc-047-traducao-das-consultas\\measure.py ^
        --before C:\\...\\measure\\baseline-validation-<before>.json ^
        --after C:\\...\\measure\\baseline-validation-<after>.json

Writes `measure.json` next to this script.
"""

from __future__ import annotations

import argparse
import io
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[1]
sys.path[:0] = [str(REPOSITORY / "backend"), str(HERE.parent / "rfc-042-fine-tuning")]

from langdetect import LangDetectException, detect  # noqa: E402
from measure_baseline import CURRENT, paired_interval, summary  # noqa: E402
from pilot import SHORT  # noqa: E402
from probe_lexicon import contains_any  # noqa: E402

from app.infrastructure.ai.clip_embedding_model import (  # noqa: E402
    TEXT_PROMPT_TEMPLATE,
)
from app.infrastructure.ai.device import resolve_device  # noqa: E402
from app.infrastructure.ai.model_runtime import (  # noqa: E402
    TRANSLATOR,
    default_runtime,
)
from app.infrastructure.ai.query_translator import (  # noqa: E402
    LEXICON,
    MarianQueryTranslator,
    fold,
    marian_input,
)

EVAL_SET = HERE.parent / "rfc-042-fine-tuning" / "eval_set.jsonl"
REFERENCE = REPOSITORY / "backend" / "tests" / "fixtures"
REFERENCE = REFERENCE / "rfc035_reference_embeddings.npz"
TIMING_ROUNDS = 200

# Risk 4 of section 11: English queries with a Portuguese name in them.
# Written here, not taken from any evaluation query.
FALSE_PORTUGUESE = (
    "beach in Rio de Janeiro",
    "aerial view of Rio de Janeiro",
    "favela on a hill in Rio de Janeiro",
    "skyline of Sao Paulo",
    "Iguazu falls from above",
    "Foz do Iguaçu seen from a drone",
    "farm in Mato Grosso",
    "Lagoa da Conceição seen from above",
    "Praia do Rosa at sunset",
    "Porto Alegre waterfront",
    "Costa Rica beach",
    "harbor of Porto",
)

# Risk 2: Portuguese queries with a place name, the new rule against the old.
PLACE_NAMES = (
    "fazenda no Mato Grosso",
    "praia em Capão da Canoa",
    "cataratas em Foz do Iguaçu",
    "Morro de São Paulo visto de cima",
    "Serra Gaúcha com vinhedos",
    "Chapada Diamantina vista de cima",
    "Porto Alegre vista de cima",
    "Rio Grande do Sul visto de cima",
    "praia em Porto Seguro",
    "lagoa em Mato Grosso do Sul",
)


# Trocas that add words, in sentences of the kind the probe does not try.
# Written here, after the validation showed "estrada de terra" and "sítio"
# going wrong (section 15): this is the evidence outside the evaluation set,
# not a change to the lexicon.
OUTSIDE_THE_PROBE = (
    "estrada de terra perto do rio",
    "uma estrada de terra entre duas fazendas",
    "estrada de terra com árvores dos dois lados",
    "estrada de chão perto da lavoura",
    "sítio arqueológico",
    "sítio histórico no centro da cidade",
    "sítio com casa e pomar",
    "farol no alto do morro",
    "farol branco e vermelho",
    "marina com veleiros",
    "barcos numa marina",
    "estádio de futebol lotado",
    "sítio arqueológico no alto do morro",
    "farol na ponta do molhe",
    "marina de iates perto do porto",
    "estrada de terra batida entre as lavouras",
    "ponte sobre o rio com estrada de chão",
    "estádio vazio",
    "mato alto na beira da estrada",
    "casa no meio do mato",
)


def old_rule(text: str) -> bool:
    """The detection of before this RFC: `langdetect` alone."""
    try:
        return bool(detect(text) == "pt")
    except LangDetectException:
        return False


def in_percent(metrics: dict[str, float]) -> dict[str, float]:
    return {k: round(100 * v, 1) for k, v in metrics.items() if k != "n"}


def interval(mine: np.ndarray, other: np.ndarray) -> dict[str, list[float]]:
    return {
        f"R@{k}": [round(100 * v, 1) for v in paired_interval(mine, other, k)]
        for k in (1, 5)
    }


def current(record: dict[str, Any]) -> dict[str, Any]:
    entry = record["candidates"][CURRENT]["templates"][TEXT_PROMPT_TEMPLATE]
    return entry["eval"]  # type: ignore[no-any-return]


def ranks(data: dict[str, Any], language: str) -> np.ndarray:
    return np.asarray(data[language]["ranks"])


def against_round_1(
    path: Path,
    after: dict[str, Any],
    chosen: list[dict[str, Any]],
    southern: list[dict[str, Any]],
    new: dict[str, Any],
) -> dict[str, Any]:
    """Section 17: the second round read against the first.

    The second round was made after the validation of the first showed what
    the probe had missed, so this is the number that is *not* clean: it is
    reported apart, and it is not a criterion.
    """
    first = json.loads(path.read_text(encoding="utf-8"))
    before = current(first)
    print("\n12. Round 2 against round 1 (post hoc: section 17)")
    report: dict[str, Any] = {"round_1": path.name}
    for language, key, group in (
        ("pt", "pt", chosen),
        ("sul", "pt_south", southern),
    ):
        was, now = ranks(before, language), ranks(new, language)
        changed = []
        for i, row in enumerate(group):
            query = row[key]
            if first["translations"][query] != after["translations"][query]:
                changed.append(
                    {
                        "query": query,
                        "round_1": first["translations"][query],
                        "round_2": after["translations"][query],
                        "rank_round_1": int(was[i]),
                        "rank_round_2": int(now[i]),
                    }
                )
        up = sum(c["rank_round_2"] < c["rank_round_1"] for c in changed)
        down = sum(c["rank_round_2"] > c["rank_round_1"] for c in changed)
        report[language] = {
            "changed": len(changed),
            "up": up,
            "down": down,
            "same_rank": len(changed) - up - down,
            "recall_round_2_vs_round_1": interval(now, was),
            "changes": changed,
        }
        print(
            f"  {language}: {len(changed)} of {len(group)} changed prompt between"
            f" rounds; up {up}, down {down}, same {len(changed) - up - down}"
        )
        for c in changed:
            print(
                f"     {c['rank_round_1']:>3} -> {c['rank_round_2']:<3} {c['query']}\n"
                f"         {c['round_1']}\n         {c['round_2']}"
            )
    for language in ("pt", "pt (sul)", "sul"):
        was, now = ranks(before, language), ranks(new, language)
        summary_1, summary_2 = summary(was), summary(now)
        delta = interval(now, was)
        report[f"recall_{language}"] = {
            "round_1": in_percent(summary_1),
            "round_2": in_percent(summary_2),
            "round_2_vs_round_1": delta,
        }
        print(
            f"  {language:9} R@1 {100 * summary_1['R@1']:5.1f} ->"
            f" {100 * summary_2['R@1']:5.1f} {delta['R@1']}   R@5"
            f" {100 * summary_1['R@5']:5.1f} -> {100 * summary_2['R@5']:5.1f}"
            f" {delta['R@5']}"
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument(
        "--round1",
        type=Path,
        help="the --after run of the first round (section 17): the second round"
        " is also read against it, apart from the criteria",
    )
    args = parser.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")

    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))
    if before["split"] != "validation" or after["split"] != "validation":
        raise SystemExit("both runs must be on the validation split")
    rows = [
        json.loads(line)
        for line in EVAL_SET.read_text(encoding="utf-8").splitlines()
        if line
    ]
    chosen = [row for row in rows if row["split"] == "validation"]
    southern = [row for row in chosen if "pt_south" in row]
    old, new = current(before), current(after)
    report: dict[str, Any] = {
        "before": args.before.name,
        "after": args.after.name,
        "queries": len(chosen),
    }

    print("1. English queries keep their prompt")
    english = [row["en"] for row in chosen]
    same = sum(
        before["translations"][q] == after["translations"][q] == q for q in english
    )
    untouched = sum(marian_input(q) is None for q in english)
    same_rank = bool((ranks(old, "en") == ranks(new, "en")).all())
    report["criterion_1"] = {
        "same_prompt": same,
        "not_translated": untouched,
        "same_ranks": same_rank,
    }
    print(
        f"  {same} of {len(english)} same prompt; {untouched} not translated;", end=""
    )
    print(f" ranks identical: {same_rank}")

    print("\n2. Portuguese queries whose prompt changed")
    criterion_2: dict[str, Any] = {}
    for language, key in (("pt", "pt"), ("sul", "pt_south")):
        group = chosen if language == "pt" else southern
        old_rank, new_rank = ranks(old, language), ranks(new, language)
        changed, steady = [], []
        for i, row in enumerate(group):
            query = row[key]
            was, now = before["translations"][query], after["translations"][query]
            item = {
                "query": query,
                "before": was,
                "after": now,
                "rank_before": int(old_rank[i]),
                "rank_after": int(new_rank[i]),
            }
            (changed if was != now else steady).append(item)
        up = sum(c["rank_after"] < c["rank_before"] for c in changed)
        down = sum(c["rank_after"] > c["rank_before"] for c in changed)
        steady_same = all(s["rank_before"] == s["rank_after"] for s in steady)
        newly = sum(
            not old_rule(row[key]) and marian_input(row[key]) is not None
            for row in group
        )
        criterion_2[language] = {
            "queries": len(group),
            "changed": len(changed),
            "up": up,
            "down": down,
            "same_rank": len(changed) - up - down,
            "unchanged_keep_their_rank": steady_same,
            "newly_translated": newly,
            "changes": changed,
        }
        print(
            f"  {language}: {len(changed)} of {len(group)} changed prompt"
            f" ({newly} newly translated); up {up}, down {down},"
            f" same {len(changed) - up - down}; the other {len(steady)} keep"
            f" their rank: {steady_same}"
        )
        for c in changed:
            print(
                f"     {c['rank_before']:>3} -> {c['rank_after']:<3} {c['query']}\n"
                f"         {c['before']}\n         {c['after']}"
            )
    report["criterion_2"] = criterion_2

    print("\n3. Recall, before and after (validation, in %)")
    recall: dict[str, Any] = {}
    for language in ("en", "pt", "pt (sul)", "sul"):
        was, now = ranks(old, language), ranks(new, language)
        recall[language] = {
            "queries": len(was),
            "before": in_percent(summary(was)),
            "after": in_percent(summary(now)),
            "after_vs_before": interval(now, was),
        }
        r1, r5 = summary(now)["R@1"], summary(now)["R@5"]
        delta = recall[language]["after_vs_before"]
        print(
            f"  {language:9} R@1 {100 * summary(was)['R@1']:5.1f} -> {100 * r1:5.1f}"
            f" {delta['R@1']}   R@5 {100 * summary(was)['R@5']:5.1f} ->"
            f" {100 * r5:5.1f} {delta['R@5']}"
        )
    gaps = {
        "pt vs en, before": interval(ranks(old, "pt"), ranks(old, "en")),
        "pt vs en, after": interval(ranks(new, "pt"), ranks(new, "en")),
        "sul vs pt (sul), before": interval(ranks(old, "sul"), ranks(old, "pt (sul)")),
        "sul vs pt (sul), after": interval(ranks(new, "sul"), ranks(new, "pt (sul)")),
    }
    recall["gaps"] = gaps
    for name, value in gaps.items():
        print(f"  {name:24} R@1 {value['R@1']}  R@5 {value['R@5']}")
    report["criterion_3"] = recall

    if args.round1:
        report["round_2_against_round_1"] = against_round_1(
            args.round1, after, chosen, southern, new
        )

    translator = MarianQueryTranslator(
        default_runtime().loader(TRANSLATOR), device=resolve_device()
    )
    translator._translate("aquecimento")

    print("\n4. The pilot's 30 single words")
    words = []
    for portuguese, english in SHORT:
        form = next(
            (f for f in LEXICON.forms if f.words == tuple(fold(portuguese).split())),
            None,
        )
        sent = marian_input(portuguese)
        out = translator.to_english(portuguese)
        words.append(
            {
                "pt": portuguese,
                "in_lexicon": form is not None,
                "translated_before": old_rule(portuguese),
                "sent": sent,
                "out": out,
                "right": form is not None and contains_any(out, form.en),
                "en": english,
                "en_unchanged": marian_input(english) is None,
            }
        )
    report["criterion_4"] = {
        "in_lexicon": sum(w["in_lexicon"] for w in words),
        "right": sum(w["right"] for w in words),
        "translated_before": sum(w["translated_before"] for w in words),
        "english_unchanged": sum(w["en_unchanged"] for w in words),
        "words": words,
    }
    for w in words:
        mark = "ok" if w["right"] else "XX"
        print(f"  {mark} {w['pt']:15} -> {w['sent']!s:22} -> {w['out']}")
    print(
        f"  {report['criterion_4']['right']} of"
        f" {report['criterion_4']['in_lexicon']} in the lexicon reach CLIP right"
        f" (translated before: {report['criterion_4']['translated_before']});"
        f" English unchanged: {report['criterion_4']['english_unchanged']} of 30"
    )

    print("\n5. Time of the lexicon and the markers, per query")
    texts = [row[k] for row in chosen for k in ("pt", "en")]
    texts += [row["pt_south"] for row in southern]
    texts += [w for pair in SHORT for w in pair]
    per_query: list[float] = []
    for _ in range(TIMING_ROUNDS):
        for text in texts:
            started = time.perf_counter()
            LEXICON.has_marker(text)
            LEXICON.rewrite(text)
            per_query.append(time.perf_counter() - started)
    detect_times: list[float] = []
    for text in texts:
        started = time.perf_counter()
        old_rule(text)
        detect_times.append(time.perf_counter() - started)
    ordered = sorted(per_query)
    timing = {
        "queries": len(texts),
        "rounds": TIMING_ROUNDS,
        "median_ms": round(1000 * statistics.median(per_query), 4),
        "p95_ms": round(1000 * ordered[int(0.95 * len(ordered))], 4),
        "max_ms": round(1000 * ordered[-1], 4),
        "langdetect_median_ms": round(1000 * statistics.median(detect_times), 3),
    }
    translated_now = [
        row["pt"]
        for row in chosen
        if not old_rule(row["pt"]) and marian_input(row["pt"]) is not None
    ]
    marian_seconds = []
    for text in translated_now:
        started = time.perf_counter()
        translator._translate(text)
        marian_seconds.append(time.perf_counter() - started)
    timing["newly_translated"] = len(translated_now)
    timing["marian_seconds_each"] = [round(s, 3) for s in marian_seconds]
    report["criterion_5"] = timing
    print(
        f"  lexicon + markers: median {timing['median_ms']} ms, p95"
        f" {timing['p95_ms']} ms, max {timing['max_ms']} ms"
        f" ({len(texts)} queries x {TIMING_ROUNDS});"
        f" langdetect median {timing['langdetect_median_ms']} ms"
    )
    print(f"  Marian on the {len(translated_now)} newly translated: {marian_seconds}")

    print("\n8. RFC-035's reference queries")
    reference = np.load(REFERENCE)
    touched = []
    for query, prompt in zip(reference["queries"], reference["prompts"], strict=True):
        query, prompt = str(query), str(prompt)
        was = query if old_rule(query) else None
        sent = marian_input(query)
        if sent != was:
            now = TEXT_PROMPT_TEMPLATE.format(query=translator.to_english(query))
            touched.append(
                {"query": query, "sent": sent, "before": prompt, "after": now}
            )
    report["rfc_035_reference"] = {
        "portuguese": len(reference["queries"]) // 2,
        "touched": len(touched),
        "prompt_changed": sum(t["before"] != t["after"] for t in touched),
        "queries": touched,
    }
    print(
        f"  {len(touched)} touched, {report['rfc_035_reference']['prompt_changed']}"
        " change prompt"
    )

    print("\n11. Risks 2 and 4")
    risks: dict[str, Any] = {}
    for name, queries in (
        ("false_portuguese", FALSE_PORTUGUESE),
        ("place_names", PLACE_NAMES),
        ("outside_the_probe", OUTSIDE_THE_PROBE),
    ):
        print(f"  {name}")
        items = []
        for query in queries:
            was = translator._translate(query) if old_rule(query) else query
            now = translator.to_english(query)
            items.append(
                {
                    "query": query,
                    "sent": marian_input(query),
                    "before": was,
                    "after": now,
                }
            )
            print(f"  {query:40} {was!r:42} -> {now!r}")
        risks[name] = items
    report["risks"] = risks

    probe = json.loads((HERE / "probe_lexicon.json").read_text(encoding="utf-8"))
    report["probe_summary"] = probe["summary"]

    target = HERE / "measure.json"
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"\nwrote {target}")


if __name__ == "__main__":
    main()
