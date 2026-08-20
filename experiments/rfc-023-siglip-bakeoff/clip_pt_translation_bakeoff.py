"""RFC-023 pre-implementation bake-off: does translating PT queries to
English before encoding close the gap for English-only CLIP text towers?

Follow-up to clip_jina_bakeoff.py and clip_small_dim_bakeoff.py, both of
which found every English-only CLIP candidate trailing SigLIP 2 `base`
badly on the Portuguese queries (native-PT strict accuracy 32-44% vs
SigLIP's 64%) -- expected, since none of those CLIP text towers were
trained on Portuguese at all. A prompt-template treatment (like the
aerial-photo template that helped SigLIP so400m) can't fix that: it's a
language-coverage gap in the tokenizer/embedding space, not a
domain-phrasing gap.

What *can* plausibly help: translating the query to English before
encoding it, i.e. treat the fast English-only CLIP models as
"translate-then-embed" rather than natively multilingual. This only adds
a query-time cost (translating one short string per search request) --
it does not touch bulk image encoding, which is where the 10x+ latency
gap between SigLIP and the fast CLIP candidates actually lives (see
clip_small_dim_bakeoff.py's README section). So the two costs are not
directly comparable: SigLIP's speed disadvantage is per-*image*, paid
once per encode of the whole (potentially 100k-image) corpus; a
translation step's cost is per-*query*, paid once per search request. If
translation is cheap and closes most of the accuracy gap, it doesn't
erase the bulk-indexing speed win the fast CLIP candidates have.

This script re-tests all five previously-run CLIP candidates (two
768-dim, three 512-dim -- everything clip_jina_bakeoff.py and
clip_small_dim_bakeoff.py covered) against three query variants per
English query:
    en       -- the original English query (already measured, included
                here again for a clean single comparison table)
    pt       -- the hand-written native Portuguese translation (already
                measured; included again for the same reason)
    pt_via_mt -- the native Portuguese text machine-translated back to
                English via Helsinki-NLP/opus-mt-ROMANCE-en, then encoded
                exactly like an English query

Translation model: `Helsinki-NLP/opus-mt-pt-en` (the direct PT-EN pair)
no longer resolves on the Hub -- verified before writing this script, not
assumed. `Helsinki-NLP/opus-mt-ROMANCE-en` (multi-source: fr/es/it/pt/...
-> en) does, tagged with the `>>por<<` language prefix Marian's
multi-source models use to disambiguate the source language. Loaded via
AutoTokenizer/AutoModelForSeq2SeqLM directly (not the `pipeline()`
helper) since this model's config doesn't register under the generic
"translation" pipeline task name.

Run:
    python clip_pt_translation_bakeoff.py
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

import torch  # noqa: E402
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer  # noqa: E402

from clip_jina_bakeoff import encode_images_clip, encode_texts_clip  # noqa: E402
from siglip_bakeoff import (  # noqa: E402
    DEMO_CORPUS_ROOT,
    DEMO_MANIFEST_PATH,
    PORTUGUESE_TRANSLATIONS,
    CandidateReport,
    QueryResult,
    load_manifest,
    load_queries,
    select_device,
)
from transformers import AutoModel, AutoProcessor  # noqa: E402

CANDIDATES: list[str] = [
    "openai/clip-vit-large-patch14",
    "laion/CLIP-ViT-L-14-laion2B-s32B-b82K",
    "openai/clip-vit-base-patch32",
    "openai/clip-vit-base-patch16",
    "laion/CLIP-ViT-B-32-laion2B-s34B-b79K",
]

TRANSLATOR_MODEL_ID = "Helsinki-NLP/opus-mt-ROMANCE-en"
PT_LANG_TAG = ">>por<<"


def load_translator():
    tokenizer = AutoTokenizer.from_pretrained(TRANSLATOR_MODEL_ID)
    model = AutoModelForSeq2SeqLM.from_pretrained(TRANSLATOR_MODEL_ID)
    model.eval()
    return tokenizer, model


def translate_pt_to_en(tokenizer, model, pt_texts: list[str]) -> tuple[dict[str, str], list[float]]:
    """Translate each PT string to English, one at a time -- matches how a
    live search query would actually be translated (a single string per
    request), not a batch-throughput best case."""
    translations: dict[str, str] = {}
    timings: list[float] = []
    for text in pt_texts:
        tagged = f"{PT_LANG_TAG} {text}"
        start = time.perf_counter()
        batch = tokenizer([tagged], return_tensors="pt", padding=True)
        with torch.no_grad():
            generated = model.generate(**batch, max_length=100)
        translations[text] = tokenizer.batch_decode(generated, skip_special_tokens=True)[0]
        timings.append(time.perf_counter() - start)
    return translations, timings


def run_candidate(
    model_id: str,
    device: torch.device,
    image_paths: list[Path],
    queries: list[dict],
    pt_via_mt: dict[str, str],
) -> CandidateReport:
    start = time.perf_counter()
    processor = AutoProcessor.from_pretrained(model_id)
    model = AutoModel.from_pretrained(model_id, dtype=torch.float32)
    model.to(device)
    model.eval()
    load_seconds = time.perf_counter() - start
    print(f"  loaded in {load_seconds:.1f}s")

    image_embeddings, image_timings = encode_images_clip(model, processor, device, image_paths)
    print(
        f"  encoded {len(image_paths)} images: mean {statistics.mean(image_timings):.3f}s  "
        f"median {statistics.median(image_timings):.3f}s"
    )

    # Three text variants per query, deduplicated (pt_via_mt occasionally
    # matches the original EN string exactly for a short/simple query).
    all_texts: list[str] = []
    for entry in queries:
        en_text = entry["query"]
        pt_text = PORTUGUESE_TRANSLATIONS.get(en_text)
        all_texts.append(en_text)
        if pt_text is not None:
            all_texts.append(pt_text)
            all_texts.append(pt_via_mt[pt_text])
    all_texts = list(dict.fromkeys(all_texts))

    text_embeddings, text_timings = encode_texts_clip(model, processor, device, all_texts)
    print(
        f"  encoded {len(all_texts)} texts: mean {statistics.mean(text_timings):.3f}s  "
        f"median {statistics.median(text_timings):.3f}s"
    )

    dim = next(iter(image_embeddings.values())).shape[0]
    report = CandidateReport(
        model_id=model_id,
        label=model_id,
        batch_size=1,
        dim=dim,
        load_seconds=load_seconds,
        image_seconds=image_timings,
        text_seconds=text_timings,
    )

    def similarity(text_vec: torch.Tensor, relative_path: str) -> float:
        image_vec = image_embeddings[DEMO_CORPUS_ROOT / relative_path]
        return float(torch.dot(text_vec, image_vec))

    for entry in queries:
        en_text = entry["query"]
        pt_text = PORTUGUESE_TRANSLATIONS.get(en_text)
        variants = [("en", en_text)]
        if pt_text is not None:
            variants.append(("pt", pt_text))
            variants.append(("pt_via_mt", pt_via_mt[pt_text]))

        for lang, query_text in variants:
            query_vec = text_embeddings[query_text]
            relevant_scores = [similarity(query_vec, p) for p in entry["relevant"]]
            hard_scores = [similarity(query_vec, p) for p in entry["hard_negatives"]]

            strict_pass = min(relevant_scores) > max(hard_scores)
            pairwise_total = len(relevant_scores) * len(hard_scores)
            pairwise_correct = sum(
                1 for r in relevant_scores for h in hard_scores if r > h
            )

            report.results.append(
                QueryResult(
                    query=en_text,
                    lang=lang,
                    strict_pass=strict_pass,
                    pairwise_correct=pairwise_correct,
                    pairwise_total=pairwise_total,
                )
            )

    del model, processor
    if device.type == "cuda":
        torch.cuda.empty_cache()
    return report


def main() -> None:
    device = select_device()

    manifest = load_manifest(DEMO_MANIFEST_PATH)
    manifest.verify_matches_directory(DEMO_CORPUS_ROOT)
    image_paths = [DEMO_CORPUS_ROOT / img.relative_path for img in manifest.images]

    queries = load_queries()

    print(f"\nLoading translator ({TRANSLATOR_MODEL_ID})...")
    start = time.perf_counter()
    tokenizer, translator_model = load_translator()
    translator_load_seconds = time.perf_counter() - start
    print(f"  loaded in {translator_load_seconds:.1f}s")

    pt_texts = list(dict.fromkeys(PORTUGUESE_TRANSLATIONS.values()))
    pt_via_mt, translation_timings = translate_pt_to_en(tokenizer, translator_model, pt_texts)
    print(
        f"  translated {len(pt_texts)} PT queries: "
        f"mean {statistics.mean(translation_timings):.3f}s/query  "
        f"median {statistics.median(translation_timings):.3f}s/query  "
        f"total {sum(translation_timings):.1f}s"
    )
    print("\nSample translations (PT -> MT English, vs the original EN query):")
    for entry in queries[:5]:
        pt_text = PORTUGUESE_TRANSLATIONS.get(entry["query"])
        if pt_text:
            print(f"  {pt_text!r}")
            print(f"    -> MT:  {pt_via_mt[pt_text]!r}")
            print(f"    -> orig EN: {entry['query']!r}")

    del translator_model
    if device.type == "cuda":
        torch.cuda.empty_cache()

    reports: list[CandidateReport] = []
    for model_id in CANDIDATES:
        print(f"\n{'=' * 70}\n{model_id}\n{'=' * 70}")
        report = run_candidate(model_id, device, image_paths, queries, pt_via_mt)
        reports.append(report)

    print(f"\n\n{'#' * 78}\nSUMMARY  (device={device}, {len(image_paths)} images, "
          f"{len(queries)} queries, translator={TRANSLATOR_MODEL_ID})"
          f"\n{'#' * 78}\n")

    header = (
        f"{'model':45} {'dim':>5} {'strict-EN':>10} {'strict-PT':>10} "
        f"{'strict-PT(MT)':>14} {'pairwise-EN':>12} {'pairwise-PT(MT)':>16}"
    )
    print(header)
    print("-" * len(header))
    for r in reports:
        pairwise_en = sum(res.pairwise_correct for res in r.results if res.lang == "en")
        pairwise_en_total = sum(res.pairwise_total for res in r.results if res.lang == "en")
        pairwise_mt = sum(res.pairwise_correct for res in r.results if res.lang == "pt_via_mt")
        pairwise_mt_total = sum(res.pairwise_total for res in r.results if res.lang == "pt_via_mt")
        print(
            f"{r.label:45} {r.dim:>5} "
            f"{r.strict_accuracy_for('en'):>10.1%} "
            f"{r.strict_accuracy_for('pt'):>10.1%} "
            f"{r.strict_accuracy_for('pt_via_mt'):>14.1%} "
            f"{pairwise_en / pairwise_en_total:>12.1%} "
            f"{pairwise_mt / pairwise_mt_total:>16.1%}"
        )

    print("\nPer-query strict failures (PT via MT):")
    for r in reports:
        failures = [res.query for res in r.results if res.lang == "pt_via_mt" and not res.strict_pass]
        print(f"  {r.label}: {failures if failures else '(none)'}")

    results_path = Path(__file__).with_name("clip_pt_translation_bakeoff_results.json")
    results_path.write_text(
        json.dumps(
            {
                "device": str(device),
                "translator_model_id": TRANSLATOR_MODEL_ID,
                "translator_load_seconds": translator_load_seconds,
                "translation_seconds_mean": statistics.mean(translation_timings),
                "translation_seconds_median": statistics.median(translation_timings),
                "image_count": len(image_paths),
                "query_count": len(queries),
                "pt_via_mt_samples": {
                    pt: en for pt, en in list(pt_via_mt.items())[:10]
                },
                "candidates": [
                    {
                        "model_id": r.model_id,
                        "dim": r.dim,
                        "strict_accuracy_en": r.strict_accuracy_for("en"),
                        "strict_accuracy_pt_native": r.strict_accuracy_for("pt"),
                        "strict_accuracy_pt_via_mt": r.strict_accuracy_for("pt_via_mt"),
                        "image_seconds_mean": statistics.mean(r.image_seconds),
                        "failing_queries_pt_via_mt": [
                            res.query
                            for res in r.results
                            if res.lang == "pt_via_mt" and not res.strict_pass
                        ],
                    }
                    for r in reports
                ],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\nFull results written to {results_path}")


if __name__ == "__main__":
    main()
