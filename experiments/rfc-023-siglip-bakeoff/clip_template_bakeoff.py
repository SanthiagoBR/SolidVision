"""RFC-023 pre-implementation bake-off: does a domain template help the fast CLIP candidates?

Follow-up to clip_small_dim_bakeoff.py and clip_pt_translation_bakeoff.py.
siglip_template_check.py already found that a domain-specific template
("an aerial photo of {query}") lifted SigLIP so400m from 33.3% to 55.6%
strict-EN, but left SigLIP base flat (44.4% either way) -- not a universal
trick, model-dependent, and never tested on any CLIP candidate. Since it
costs nothing (no retraining, just a different string before encoding),
it is worth checking empirically rather than assuming either way.

Tests three template variants (matching siglip_template_check.py's
variants exactly, for comparability) on the three 512-dim CLIP candidates
clip_small_dim_bakeoff.py found fast enough to be worth considering, on
both the English queries and the Portuguese-via-machine-translation
queries clip_pt_translation_bakeoff.py already validated -- native
Portuguese is skipped here since wrapping non-English text in an English
template phrase would just produce a code-mixed string, not a meaningful
test of either language.

Image embeddings do not depend on the text template, so each model's 45
images are encoded exactly once and reused across all three template
variants, matching siglip_template_check.py's own cost-saving approach.

Run:
    python clip_template_bakeoff.py
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

import torch  # noqa: E402
from transformers import AutoModel, AutoProcessor  # noqa: E402

from clip_jina_bakeoff import encode_images_clip  # noqa: E402
from clip_pt_translation_bakeoff import load_translator, translate_pt_to_en  # noqa: E402
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

CANDIDATES: list[str] = [
    "openai/clip-vit-base-patch32",
    "openai/clip-vit-base-patch16",
    "laion/CLIP-ViT-B-32-laion2B-s34B-b79K",
]

TEMPLATE_VARIANTS: dict[str, str] = {
    "raw": "{query}",
    "a_photo_of": "a photo of {query}",
    "aerial_photo_of": "an aerial photo of {query}",
}


def encode_text_clip(model, processor, device: torch.device, text: str) -> torch.Tensor:
    inputs = processor(text=[text], padding=True, truncation=True, return_tensors="pt").to(device)
    with torch.no_grad():
        output = model.get_text_features(**inputs)
    features = output.pooler_output
    return (features / features.norm(p=2, dim=-1, keepdim=True)).squeeze(0).cpu()


def score_variant(
    template: str,
    queries: list[dict],
    pt_via_mt: dict[str, str],
    image_embeddings: dict[Path, torch.Tensor],
    model,
    processor,
    device: torch.device,
) -> CandidateReport:
    report = CandidateReport(model_id="", label="", batch_size=1, dim=0, load_seconds=0.0)

    def similarity(text_vec: torch.Tensor, relative_path: str) -> float:
        image_vec = image_embeddings[DEMO_CORPUS_ROOT / relative_path]
        return float(torch.dot(text_vec, image_vec))

    for entry in queries:
        en_text = template.format(query=entry["query"])
        pt_native = PORTUGUESE_TRANSLATIONS.get(entry["query"])
        variants = [("en", en_text)]
        if pt_native is not None:
            pt_mt_text = template.format(query=pt_via_mt[pt_native])
            variants.append(("pt_via_mt", pt_mt_text))

        for lang, text in variants:
            query_vec = encode_text_clip(model, processor, device, text)
            relevant_scores = [similarity(query_vec, p) for p in entry["relevant"]]
            hard_scores = [similarity(query_vec, p) for p in entry["hard_negatives"]]

            strict_pass = min(relevant_scores) > max(hard_scores)
            pairwise_total = len(relevant_scores) * len(hard_scores)
            pairwise_correct = sum(
                1 for r in relevant_scores for h in hard_scores if r > h
            )

            report.results.append(
                QueryResult(
                    query=entry["query"],
                    lang=lang,
                    strict_pass=strict_pass,
                    pairwise_correct=pairwise_correct,
                    pairwise_total=pairwise_total,
                )
            )

    return report


def pairwise_for(report: CandidateReport, lang: str) -> float:
    correct = sum(r.pairwise_correct for r in report.results if r.lang == lang)
    total = sum(r.pairwise_total for r in report.results if r.lang == lang)
    return correct / total if total else float("nan")


def run_candidate(
    model_id: str,
    device: torch.device,
    image_paths: list[Path],
    queries: list[dict],
    pt_via_mt: dict[str, str],
) -> dict[str, CandidateReport]:
    print(f"\n{'=' * 70}\n{model_id}\n{'=' * 70}")
    start = time.perf_counter()
    processor = AutoProcessor.from_pretrained(model_id)
    model = AutoModel.from_pretrained(model_id, dtype=torch.float32)
    model.to(device)
    model.eval()
    print(f"  loaded in {time.perf_counter() - start:.1f}s")

    start = time.perf_counter()
    image_embeddings, _ = encode_images_clip(model, processor, device, image_paths)
    print(f"  encoded {len(image_paths)} images in {time.perf_counter() - start:.1f}s")

    variant_reports: dict[str, CandidateReport] = {}
    for variant_name, template in TEMPLATE_VARIANTS.items():
        report = score_variant(template, queries, pt_via_mt, image_embeddings, model, processor, device)
        variant_reports[variant_name] = report
        print(
            f"  [{variant_name:16}] template={template!r:32} "
            f"strict-EN={report.strict_accuracy_for('en'):>6.1%}  "
            f"strict-PT(MT)={report.strict_accuracy_for('pt_via_mt'):>6.1%}  "
            f"pairwise-EN={pairwise_for(report, 'en'):>6.1%}  "
            f"pairwise-PT(MT)={pairwise_for(report, 'pt_via_mt'):>6.1%}"
        )

    del model, processor
    if device.type == "cuda":
        torch.cuda.empty_cache()
    return variant_reports


def main() -> None:
    device = select_device()

    manifest = load_manifest(DEMO_MANIFEST_PATH)
    manifest.verify_matches_directory(DEMO_CORPUS_ROOT)
    image_paths = [DEMO_CORPUS_ROOT / img.relative_path for img in manifest.images]
    queries = load_queries()

    print(f"\nLoading translator...")
    tokenizer, translator_model = load_translator()
    pt_texts = list(dict.fromkeys(PORTUGUESE_TRANSLATIONS.values()))
    pt_via_mt, _ = translate_pt_to_en(tokenizer, translator_model, pt_texts)
    print(f"  translated {len(pt_texts)} PT queries")
    del translator_model
    if device.type == "cuda":
        torch.cuda.empty_cache()

    all_results: dict[str, dict[str, CandidateReport]] = {}
    for model_id in CANDIDATES:
        all_results[model_id] = run_candidate(model_id, device, image_paths, queries, pt_via_mt)

    print(f"\n\n{'#' * 100}\nSUMMARY: strict accuracy by template variant\n{'#' * 100}\n")
    header = f"{'model':42} {'variant':18} {'strict-EN':>10} {'strict-PT(MT)':>14} {'pairwise-EN':>12} {'pairwise-PT(MT)':>16}"
    print(header)
    print("-" * len(header))
    for model_id, variants in all_results.items():
        for variant_name, report in variants.items():
            print(
                f"{model_id:42} {variant_name:18} "
                f"{report.strict_accuracy_for('en'):>10.1%} "
                f"{report.strict_accuracy_for('pt_via_mt'):>14.1%} "
                f"{pairwise_for(report, 'en'):>12.1%} "
                f"{pairwise_for(report, 'pt_via_mt'):>16.1%}"
            )

    results_path = Path(__file__).with_name("clip_template_bakeoff_results.json")
    results_path.write_text(
        json.dumps(
            {
                "device": str(device),
                "image_count": len(image_paths),
                "query_count": len(queries),
                "template_variants": TEMPLATE_VARIANTS,
                "candidates": {
                    model_id: {
                        variant_name: {
                            "strict_accuracy_en": report.strict_accuracy_for("en"),
                            "strict_accuracy_pt_via_mt": report.strict_accuracy_for("pt_via_mt"),
                            "pairwise_accuracy_en": pairwise_for(report, "en"),
                            "pairwise_accuracy_pt_via_mt": pairwise_for(report, "pt_via_mt"),
                            "failing_queries_en": [
                                r.query for r in report.results if r.lang == "en" and not r.strict_pass
                            ],
                            "failing_queries_pt_via_mt": [
                                r.query for r in report.results if r.lang == "pt_via_mt" and not r.strict_pass
                            ],
                        }
                        for variant_name, report in variants.items()
                    }
                    for model_id, variants in all_results.items()
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nFull results written to {results_path}")


if __name__ == "__main__":
    main()
