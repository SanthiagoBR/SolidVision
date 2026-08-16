# RFC-023 pre-implementation bake-off

Exploration tooling and results, not application code. This directory
exists to answer one question before any RFC-023 (SigLIP Adapter) code is
written: **which SigLIP 2 checkpoint should `settings.embedding_model`
default to?**

Kept off `develop` deliberately -- see the branch this lives on
(`explore/siglip-bakeoff`). Nothing here is imported by `backend/app/`, and
none of it is meant to be merged as-is. Whatever RFC-023 actually needs
(a chosen checkpoint, a verified dimension, measured latency figures) gets
written into the RFC and the real adapter on `develop`; this directory is
the working notes behind that decision.

## Why 1152 (`vector(1152)`, migration `999b801e80f4`) constrains the choice

`embedding_dimension` is committed at 1152 and matches the SigLIP so400m
hidden size in both SigLIP generations. That already ruled out the base
(768-dim) and large (1024-dim) checkpoints on paper -- until it became
clear that changing the dimension today costs one Alembic migration and a
45-image re-seed, versus a full production reindex later. Cheap enough
now to be worth re-litigating with actual data instead of assuming so400m
by default. See `docs/rfcs/rfc-022-demo-dataset.md` §7.1/§7.4 for why the
demo corpus and `queries.json` exist in the first place -- this bake-off
is the first real consumer of both.

## `siglip_bakeoff.py`

Loads each of three SigLIP 2 checkpoints, encodes all 45 demo-corpus
images and 18 queries (9 English from `backend/dataset/demo/queries.json`
+ 9 hand-written Portuguese translations -- the product README advertises
Portuguese search), and scores against the committed ground truth using
the exact predicate already sitting dormant in
`backend/tests/dataset/test_queries.py::test_relevant_images_rank_above_hard_negatives`.

Auto-detects CUDA vs CPU and locates the repo root on its own, so it runs
unmodified on any machine inside any checkout.

### Results — CPU-only laptop (`results_cpu_laptop.json`)

| Checkpoint | Dim | Strict-EN | Strict-PT | Pairwise | s/image (mean) | Load time | Projected 100k images |
|---|---|---|---|---|---|---|---|
| `siglip2-so400m-patch14-384` | 1152 | 33.3% | 55.6% | 87.8% | 22.72s | 109.4s | 26.3 days |
| `siglip2-large-patch16-384` | 1024 | 33.3% | 44.4% | 81.1% | 11.11s | 97.4s | 12.9 days |
| `siglip2-base-patch16-384` | 768 | **44.4%** | 55.6% | 79.3% | **3.66s** | **8.8s** | **4.2 days** |

*Strict = every relevant image outranks every hard negative for that
query. Pairwise = fraction of individual relevant/hard-negative
comparisons ordered correctly -- softer, since several queries have up
to 6 relevant images against 2-3 hard negatives, so one weak match tanks
the strict score. 100k projection is single-process, unbatched, CPU --
not a claim about production throughput, which needs batching (explicit
future work, not RFC-023 scope).*

**Finding:** `base` had the *highest* strict accuracy of the three while
being ~6x faster than `so400m`. `so400m` only leads on the softer
pairwise metric. Bigger did not mean better at the metric that actually
matters here on this corpus.

**Caveats, not swept under the rug:**
- 9 English queries means each one is worth ~11 percentage points --
  the direction (base >= so400m/large on strict) is a real pattern in
  this run, the exact percentages are not precise.
- The same ~5-6 queries fail across all three models (the
  lake-vs-pond-vs-pool distinction, warehouse-vs-farmland, several urban
  categories). RFC-022 §6.1 built those hard negatives to be genuinely
  hard; consistent failure across checkpoints reads as the eval set
  doing its job, not a broken pipeline.
- Portuguese did not underperform English on any checkpoint -- doesn't
  prove PT is *better* (same small-sample caveat), but it does clear the
  concern that motivated picking SigLIP 2 (multilingual) over SigLIP 1
  (English-only) in the first place.

## `siglip_template_check.py`

Follow-up check on the two poles (`so400m` and `base`) from the main
bake-off, testing whether a CLIP-style caption template changes the
strict-accuracy numbers above. SigLIP's own paper claims no template is
needed, trained as it was on raw web captions unlike CLIP -- this either
confirms that claim against this project's corpus, or overturns an
assumption before it becomes load-bearing in the RFC.

### Results (`template_check_results.json`)

| Checkpoint | `{query}` (raw) | `"a photo of {query}"` | `"an aerial photo of {query}"` |
|---|---|---|---|
| `so400m` strict | 33.3% | 33.3% | **55.6%** |
| `so400m` pairwise | 85.4% | 85.4% | 89.0% |
| `base` strict | **44.4%** | 33.3% | 44.4% |
| `base` pairwise | 72.0% | 69.5% | 69.5% |

**This is not a clean confirmation of the "no template needed" claim.**
A generic CLIP-style template ("a photo of ...") does nothing for either
checkpoint -- flat or worse. But a *domain-specific* template ("an aerial
photo of ...") lifts `so400m` from 33.3% to 55.6% strict accuracy, which
is now the single best result across every checkpoint/template
combination tested, `base` included. `base` is unmoved by the aerial
template either way (44.4% with or without it).

This reopens the speed/quality tradeoff rather than closing it:
`so400m` + the aerial template now beats `base`'s best result on the
metric that matters, but still costs ~6.2x the per-image latency
(22.7s vs 3.7s) and ~12.4x the load time (109s vs 8.8s) measured in the
main bake-off. Adopting a hardcoded domain template also isn't free in
another sense -- it would mean `SearchImagesUseCase` wraps every user
query in "an aerial photo of ..." before encoding, which only makes
sense while the corpus stays aerial-photography-specific; ARCHITECTURE.md
§9's stated direction (future adapters, possibly future non-aerial
collections) makes that a real generality cost, not just an
implementation detail.

Same small-sample caveat as the main bake-off applies with more force
here: 9 queries means the aerial-template jump for `so400m` is exactly 2
queries flipping from fail to pass. Real, but not a lot of queries to
generalize a product decision from.

## Reproducing on different hardware

`siglip_bakeoff.py` is written to run unmodified wherever it's placed
inside a checkout of this repo. See the setup instructions in its own
docstring. A second results file was expected from a friend's NVIDIA GPU
machine as an additional hardware data point; add it here as
`results_<description>.json` if/when it lands, following the same naming
pattern as `results_cpu_laptop.json`.
