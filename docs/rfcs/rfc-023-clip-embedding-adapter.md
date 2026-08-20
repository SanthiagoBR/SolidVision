# RFC-023 — CLIP Embedding Adapter

**Status:** Proposed
**Author:** SolidVision
**Depends on:** RFC-015 (Index Use Case), RFC-018 (Embedding Column + HNSW), RFC-021 (Indexing Worker), RFC-022 (Demo Dataset)
**Blocks:** Vector search RFC, benchmark RFC, fine-tuning RFC
**Last updated:** 2026-08-20

---

# 1. Context

Every layer of the indexing pipeline is real except the one that gives it meaning.

`FilesystemImageProvider` discovers files. `IndexingWorker` drives `IndexOrUpdateImageUseCase`. `PostgresImageRepository.save_indexed()` writes an image row and its embedding into a pgvector column indexed by HNSW. RFC-022 supplied a reproducible 45-image corpus with 25 ground-truth queries to run it all against.

But the only `EmbeddingModelPort` implementation in the codebase is `FakeEmbeddingModel`, which hashes the image's id and path with SHA-256 and never opens the file. It produces well-shaped vectors with correct geometry and zero semantic content. Everything downstream — similarity, ranking, recall — is measuring noise.

This RFC replaces it in production with a real model.

## 1.1 What the bake-off was

`experiments/rfc-023-siglip-bakeoff/` compared SigLIP2, several CLIP checkpoints, and jina-CLIP across embedding dimension, text-prompt template, Portuguese handling, INT8 quantization, and batching. Every number in section 3 is reproduced from those runs.

**Artifact caveat, stated up front:** the bake-off's Python scripts and its README are **not present in the repository**. `experiments/` is entirely untracked, and `.gitignore` excludes `*.log`, so nothing from it is in git either. What survives on disk is the set of run logs:

| Log | Covers |
| --- | --- |
| `clip_small_dim_run_output.log` | 512-dim CLIP checkpoints: accuracy + throughput |
| `clip_jina_run_output.log` | 768-dim CLIP + jina-clip-v2 |
| `clip_pt_translation_run_output.log` | Portuguese: raw vs machine-translated |
| `clip_template_run_output.log` | Prompt template variants |
| `quantization_check_output.log` | SigLIP2 fp32 vs INT8 dynamic |
| `batching_check_output.log` | SigLIP2 image/text batch sizes |
| `onnx_quantization_check_output.log`, `step1_export.log`, `step2_quantize*.log` | ONNX export + static quantization attempt |

Numbers below are quoted from these logs verbatim. Anything not established by them is marked explicitly as an implementation decision or an open question. Notably, the standalone SigLIP bake-off log is missing; the SigLIP2 accuracy figures cited here come from the fp32 baseline inside `quantization_check_output.log`, which ran the same 45-image corpus.

---

# 2. Problem

Three things must be true at once, and they pull against each other.

1. **The model must be good enough to rank aerial property imagery** from Portuguese and English queries.
2. **It must be fast enough on CPU** to index toward the 100,000-image target in `ARCHITECTURE.md` §22 without a GPU budget.
3. **It must not leak into Domain or Application.** The current model is a starting point, not a commitment; replacing it must not touch a single contract above Infrastructure.

The third is the one that outlives this RFC.

---

# 3. Empirical Model Selection

All runs: CPU, 45 images from the RFC-022 demo corpus, 25 ground-truth queries from `queries.json`.

Metrics as reported by the bake-off scripts:
- **strict-EN / strict-PT** — the top-1 result is a declared `relevant` image for that query.
- **pairwise** — fraction of (relevant, hard-negative) pairs the model ranks correctly.
- **strict-PT(MT)** — Portuguese query machine-translated to English first.

## 3.1 512-dimension CLIP checkpoints

From `clip_small_dim_run_output.log`:

| model | dim | strict-EN | strict-PT | pairwise | s/img (mean) | load s |
| --- | --- | --- | --- | --- | --- | --- |
| openai/clip-vit-base-patch32 | 512 | 44.0% | 44.0% | 68.0% | 0.770 | 4.7 |
| openai/clip-vit-base-patch16 | 512 | 56.0% | 36.0% | 70.9% | 1.745 | 4.1 |
| **laion/CLIP-ViT-B-32-laion2B-s34B-b79K** | **512** | **56.0%** | **44.0%** | **74.3%** | **0.464** | **3.8** |

Projected single-process CPU wall-clock to encode 100,000 images:

| model | hours | days |
| --- | --- | --- |
| openai/clip-vit-base-patch32 | 21.39 | 0.89 |
| openai/clip-vit-base-patch16 | 48.49 | 2.02 |
| laion/CLIP-ViT-B-32-laion2B-s34B-b79K | **12.90** | **0.54** |

The LAION B-32 checkpoint is best on every axis simultaneously — accuracy, pairwise, and speed. It is not a trade-off; it dominates.

## 3.2 768-dimension checkpoints

From `clip_jina_run_output.log`:

| model | dim | strict-EN | strict-PT | pairwise | s/img (mean) | 100k hours |
| --- | --- | --- | --- | --- | --- | --- |
| openai/clip-vit-large-patch14 | 768 | 56.0% | 44.0% | 72.5% | 7.694 | 213.71 |
| laion/CLIP-ViT-L-14-laion2B-s32B-b82K | 768 | 52.0% | 32.0% | 71.2% | 6.751 | 187.54 |
| jinaai/jina-clip-v2 | 768 | 16.0% | 16.0% | 56.6% | 48.674 | 1352.06 |

Doubling the dimension bought nothing. The best 768-dim model matched the 512-dim LAION B-32 on strict-EN (56.0%) and lost on pairwise (72.5% vs 74.3%), while costing **16.6× more CPU time per image**. `jina-clip-v2` was catastrophic on this corpus — 16.0% strict accuracy, worse than several of its own trivial negatives, at 48.7 s/image.

## 3.3 Prompt template

From `clip_template_run_output.log`, on the selected checkpoint:

| variant | template | strict-EN | strict-PT(MT) | pairwise-EN | pairwise-PT(MT) |
| --- | --- | --- | --- | --- | --- |
| raw | `{query}` | 56.0% | 52.0% | 78.8% | 72.5% |
| **a_photo_of** | **`a photo of {query}`** | **60.0%** | **56.0%** | **80.4%** | **75.1%** |
| aerial_photo_of | `an aerial photo of {query}` | 52.0% | 44.0% | 73.5% | 69.3% |

`a photo of {query}` wins on all four columns. The counter-intuitive result is that **the aerial-specific template is the worst of the three** — despite this being an aerial-imagery product — costing 8 points of strict-EN and 12 of strict-PT(MT) against the generic template. The same ordering held for `openai/clip-vit-base-patch16` (52.0% → 52.0% → 52.0% strict-EN, but 78.3% → 72.5% pairwise-EN for aerial).

This is why section 7 forbids the aerial template by name.

## 3.4 Portuguese

From `clip_pt_translation_run_output.log` (translator: `Helsinki-NLP/opus-mt-ROMANCE-en`):

| model | strict-EN | strict-PT (raw) | strict-PT (MT) |
| --- | --- | --- | --- |
| openai/clip-vit-large-patch14 | 56.0% | 44.0% | **64.0%** |
| laion/CLIP-ViT-L-14-laion2B-s32B-b82K | 52.0% | 32.0% | **60.0%** |
| openai/clip-vit-base-patch32 | 44.0% | 44.0% | **52.0%** |
| openai/clip-vit-base-patch16 | 56.0% | 36.0% | **52.0%** |
| laion/CLIP-ViT-B-32-laion2B-s34B-b79K | 56.0% | 44.0% | **52.0%** |

Translating first improved every model — by 8 points on the selected checkpoint (44.0% → 52.0%), and by up to 28 points on `laion/CLIP-ViT-L-14`. CLIP's text tower is English-only; feeding it Portuguese is measurably worse than translating first.

Translation cost, same log: 25 queries in 23.5s total, **mean 0.938 s/query, median 0.859**, model load 4.8s. That is per *query*, not per image, and only for queries actually detected as Portuguese.

Sample translations recorded in the log show the quality is adequate but not perfect:

| Portuguese | Machine translation | Original English |
| --- | --- | --- |
| propriedade rural com um pequeno lago | Rural property with a small lake | rural property with a small lake |
| pequeno lago natural em uma fazenda, nao um tanque artificial | Small natural lake on a farm, not an artificial **tank** | ...not an artificial **pond** |
| tanques artificiais de piscicultura em um vale | Artificial fish **tanks** in a valley | artificial fish farming **ponds** in a valley |

The `tanque` → `tank`/`pond` confusion is a real, visible source of the remaining Portuguese gap.

## 3.5 Why CLIP and not SigLIP

SigLIP2 was the original candidate — hence the directory name. From the fp32 baseline in `quantization_check_output.log`, `google/siglip2-base-patch16-384` on the same 45-image corpus:

| | SigLIP2-base-384 (fp32) | laion CLIP-ViT-B-32 (`a photo of`) |
| --- | --- | --- |
| strict-EN | 60.0% | **60.0%** |
| strict-PT | **64.0%** (native, no translation) | 56.0% (via machine translation) |
| pairwise | 80.4% | **80.4%** (EN) |
| s/image (mean, CPU) | 4.960 | **0.464** |
| 100k images | 137.78 h (5.74 days) | **12.90 h (0.54 days)** |

The decision in one line: **identical English accuracy and identical pairwise accuracy at roughly one-tenth the CPU cost.**

SigLIP2 genuinely wins on Portuguese — 64.0% natively versus 56.0% for CLIP-plus-translation, and without needing a second model at all. That is a real advantage and it is being given up deliberately. It does not survive contact with the throughput requirement: at 4.96 s/image, indexing 100,000 images takes most of a week on one CPU process, against half a day for CLIP. For a product whose stated target is 100,000 images and whose deployment assumption is CPU, an 8-point Portuguese gap is the cheaper price.

**Caveat on comparability:** the two figures were produced by different bake-off scripts against the same corpus. The `strict-PT` columns are not measuring the same thing — SigLIP2's is native multilingual understanding, CLIP's is post-translation. `strict-EN` and `pairwise` are directly comparable; `strict-PT` is a comparison of *approaches*, not of models.

## 3.6 Why 512 dimensions

Not chosen for its own sake — it is the projection width of the winning checkpoint. But section 3.2 confirms it costs nothing: no 768-dim model in the bake-off beat the 512-dim LAION B-32 on pairwise accuracy, and all were 14–105× slower per image. Smaller vectors additionally mean a smaller HNSW index and less I/O per row.

## 3.7 Quantization and batching: measured, then rejected

**INT8 dynamic quantization** (`quantization_check_output.log`, SigLIP2, `torch.quantization.quantize_dynamic` over `nn.Linear`, onednn backend):

| metric | fp32 | int8 | change |
| --- | --- | --- | --- |
| s/image (mean) | 4.960 | 3.437 | 1.44× faster |
| strict accuracy (EN) | 60.0% | 48.0% | **−12 pts** |
| strict accuracy (PT) | 64.0% | 40.0% | **−24 pts** |
| pairwise accuracy | 80.4% | 66.4% | **−14 pts** |

Embedding drift versus fp32 on identical input: images mean cosine **0.7582** (min 0.6779), texts mean 0.9010 (min 0.7173). A 1.44× speedup that moves image embeddings by 0.24 cosine and destroys 24 points of Portuguese accuracy is not a trade worth making. **Quantization: rejected.**

**Batching** (`batching_check_output.log`, SigLIP2, CPU):

| batch size | s/image | speedup |
| --- | --- | --- |
| 1 | 3.850 | 1.00× |
| 4 | 3.412 | 1.13× |
| 32 | 3.282 | **1.17×** |

Embedding drift across every batch size versus batch-of-1 was exactly 1.0000 — batching is numerically safe. But 1.17× at the best batch size does not justify reshaping `EmbeddingModelPort`, `IndexOrUpdateImageUseCase`, and `IndexingWorker` around a batch API. Text batching was more promising (0.788 → 0.291 s/text, 2.7×) but text is encoded once per *query*, not once per image, so it is not the bottleneck. **Batching: out of scope**, and left as future work in section 17.

**ONNX + static quantization** was attempted and did not complete. `step1_export.log` shows both towers exporting successfully (`vision_fp32.onnx`, `text_fp32.onnx`) via the legacy TorchScript exporter. `step2_quantize_v2.log` shows `quantize_static()` being entered with the note that it "grew unboundedly before (microsoft/onnxruntime#21979)" and produced no results afterward. No ONNX numbers exist, and none are claimed here.

---

# 4. Decision

| Aspect | Decision |
| --- | --- |
| Model | `laion/CLIP-ViT-B-32-laion2B-s34B-b79K` |
| Embedding dimension | 512 |
| Image encoding | Native CLIP image encoding on RGB pixels |
| Text template | `a photo of {query}` — exactly |
| Portuguese | Detect, translate to English, then template |
| Translation model | `Helsinki-NLP/opus-mt-ROMANCE-en` with the `>>por<<` source token |
| Translation loading | `AutoTokenizer` + `AutoModelForSeq2SeqLM` |
| Language detection | `langdetect` with `DetectorFactory.seed = 0` |
| Quantization | None |
| Batching | None |
| Default device | `auto` → CUDA when available, else CPU |
| Vector search | Not in this RFC |
| Fine-tuning | Not in this RFC |

---

# 5. Architecture

## 5.1 The boundary

```
Domain          EmbeddingModelPort  (ABC: encode_image, encode_text)
                        ▲
                        │ implements
Infrastructure  ClipEmbeddingModel ──▶ MarianQueryTranslator
                        │                      │
                        ▼                      ▼
                  torch, transformers,  langdetect, Marian MT
                  Pillow, CLIP
```

`EmbeddingModelPort` was **not modified**. It mentions no checkpoint, tensor, tokenizer, processor, or language. `encode_text(text: str)` gained no language parameter: the language decision is an implementation detail of the current model, not a fact about the domain, and a future multilingual model would have to carry a meaningless argument forever.

Everything model-specific lives under `backend/app/infrastructure/ai/`:

| File | Responsibility |
| --- | --- |
| `clip_embedding_model.py` | The adapter: `EmbeddingModelPort` over CLIP |
| `query_translator.py` | `QueryTranslator` ABC + `MarianQueryTranslator` |
| `device.py` | `resolve_device()` — one place, for all adapters |
| `fake_embedding_model.py` | Unchanged. Still the test double |

This is enforced, not merely intended: `backend/tests/test_ai_layer_boundaries.py` walks the real source of `app/domain/` and `app/application/` and fails on any import of `torch`, `transformers`, `langdetect`, `PIL`, or `numpy`, or on any module path containing `clip`/`siglip`/`huggingface`. It also asserts the port's own text is free of implementation vocabulary.

## 5.2 `ClipEmbeddingModel` is not re-exported from the package `__init__`

`app/infrastructure/ai/__init__.py` deliberately exports only `FakeEmbeddingModel`. Most of the fast test suite imports the fake, and a package `__init__` that also pulled in the CLIP adapter would drag `torch` and `transformers` into every one of those imports for no benefit. The adapter is imported by full module path.

---

# 6. Correct Embedding Extraction

This is the part of RFC-023 most likely to be got wrong, and it was verified experimentally against the installed **transformers 5.15.0**, not copied from an example.

**`get_image_features()` and `get_text_features()` do not return a tensor.**

They return a `BaseModelOutputWithPooling` whose `.pooler_output` field the method has *overwritten* with the projected embedding:

```python
# transformers/models/clip/modeling_clip.py, v5.15.0
pooled_output = vision_outputs.pooler_output
vision_outputs.pooler_output = self.visual_projection(pooled_output)
return vision_outputs
```

The docstring example *in that same file* still reads `image_features = model.get_image_features(**inputs)` — following it produces an object, not an embedding. Measured directly:

| expression | result |
| --- | --- |
| `type(get_image_features(...))` | `BaseModelOutputWithPooling` |
| `.pooler_output.shape` | `(1, 512)` ✅ |
| `.last_hidden_state.shape` | `(1, 50, 768)` ❌ wrong space, wrong size |
| `config.projection_dim` | `512` |

The adapter reads `.pooler_output` and validates the shape. A unit test feeds it a stub whose `last_hidden_state` is 768-wide precisely so that reading the wrong field fails loudly rather than silently persisting garbage.

## 6.1 Normalization

**Measured: CLIP features are not normalized.** L2 norms on the selected checkpoint were **~11.56 for images** and **~8.47 for text**.

The HNSW index is built with `vector_cosine_ops`, and cosine similarity is the metric the future search will use. The adapter therefore L2-normalizes in `_to_embedding_vector()`, so every stored vector is unit-norm and cosine similarity reduces to a dot product.

**Normalization was deliberately not added to `EmbeddingVector`.** That value object is model-agnostic; baking an L2-unit-norm invariant into it would impose one embedding space's convention on every future model, including ones for which it is wrong. The convention belongs to the adapter that knows the model.

## 6.2 Determinism

Verified: repeated `get_image_features()` calls on the same input are bitwise identical, and Marian decoding is greedy (`num_beams=1, do_sample=False`), so the same query always yields the same English string and therefore the same embedding.

---

# 7. Text Pipeline

```
user query
    ↓  langdetect (seed 0)
    ↓  Portuguese?  ──yes──▶  Marian, ">>por<< {query}"  ──▶  English
    ↓  no
"a photo of {query}"
    ↓  CLIP text encoder
    ↓  .pooler_output → L2 normalize
512-dimensional EmbeddingVector
```

The template is exactly `a photo of {query}`. No aerial variant (section 3.3). No prompt ensembling — the bake-off tested single templates only, and averaging multiple prompts is unmeasured here.

## 7.1 Language detection

`langdetect` was chosen over a heavyweight NLP framework because the project has no such dependency and this problem does not justify introducing one. It is pure Python with one transitive dependency (`six`).

`DetectorFactory.seed = 0` is set at module import. Without it, `langdetect`'s probabilistic algorithm is not reproducible across runs — the same determinism requirement that already rules out Python's salted `hash()` in `FakeEmbeddingModel`.

**Known limitation, measured and documented rather than papered over.** `langdetect` needs several words. Full-sentence queries classify correctly in both languages, but single words do not:

| query | detected | translated? |
| --- | --- | --- |
| `propriedade rural com um pequeno lago` | `pt` | yes ✅ |
| `rural property with a small lake` | `en` | no ✅ |
| `vista aerea de uma cidade` | `pt` | yes ✅ |
| `fazenda` | `tr` | **no** ❌ |
| `lago` | `tl` | **no** ❌ |
| `piscina` | `it` | **no** ❌ |
| `farm` | `sv` | no (harmless) |

The failure mode is benign in shape: a misclassified short query is never detected as `pt`, so it falls through untranslated rather than being translated wrongly. A one-word Portuguese query reaches CLIP in Portuguese and performs as the raw-PT column in section 3.4 predicts. Building a better detector is out of scope; `test_single_word_portuguese_is_a_known_detection_gap` pins the behavior so that adopting one later is a visible, deliberate change.

`langdetect` raises `LangDetectException` on featureless input (empty strings, bare digits, punctuation). That is caught and treated as English — there is nothing to translate, and failing a query over it would be absurd.

## 7.2 Why not `pipeline("translation")`

`Helsinki-NLP/opus-mt-ROMANCE-en` is not reliably registered under that generic task in transformers 5.15. The explicit `AutoTokenizer` + `AutoModelForSeq2SeqLM` pair also keeps the `>>por<<` source token and the greedy decoding parameters visible in the source rather than buried in pipeline defaults.

`opus-mt-ROMANCE-en` is many-to-one across the Romance languages, so the source language is selected by a leading target token, not by the checkpoint. Omitting `>>por<<` would let the model guess.

---

# 8. Device Strategy

`settings.device` default changed from `cpu` to **`auto`**, resolved centrally by `app/infrastructure/ai/device.py`:

| configured | result |
| --- | --- |
| `auto` (default) | CUDA if `torch.cuda.is_available()`, else CPU |
| `cuda` when CUDA is absent | **CPU, with a warning** — not an exception |
| `cpu` | CPU, even when CUDA exists |
| anything else | passed to `torch.device` verbatim |

CUDA is an optimization, never a requirement. A machine configured for CUDA that loses it — a CPU-only wheel, a container without the runtime — must still start and serve slowly rather than fail to construct the adapter. Non-CUDA GPU backends (ROCm, MPS, XPU) are out of scope and are neither detected nor special-cased.

Both the CLIP model and the translation model receive the same resolved device; the adapter passes its device down to the translator it constructs.

---

# 9. Model Lifecycle

Two constraints in tension: models must not reload per call, and the test suite must never download them.

**The eager module-level singleton pattern from RFC-016 is not acceptable here.** `app.presentation.dependencies` is imported transitively by most of the suite (any import of `app.presentation.api` reaches it). An eager `ClipEmbeddingModel()` at import time would be a landmine.

Resolution, in two layers:

1. **`@lru_cache(maxsize=1)` on `get_embedding_model()`** — load-once/reuse-forever semantics, deferred to first call.
2. **The adapter's `__init__` is itself free.** Both the CLIP checkpoint and the translation model load on first `encode_*`, not in the constructor. The translator loads only on the first query actually detected as Portuguese, so an English-only deployment never pays for it at all.

Together these mean that neither importing the dependencies module nor *calling the provider* touches Hugging Face. `test_resolving_the_embedding_model_downloads_nothing` asserts exactly that.

`InMemoryImageRepository`'s existing eager singleton is left alone — it has no expensive I/O.

**Verified:** the entire fast suite passes with `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, and `HF_HOME` pointed at an empty directory. 297 passed.

---

# 10. Configuration

| setting | before | after |
| --- | --- | --- |
| `embedding_model` | `google/siglip-base-patch16-224` | `laion/CLIP-ViT-B-32-laion2B-s34B-b79K` |
| `embedding_dimension` | `1152` | `512` |
| `device` | `cpu` | `auto` |

`.env.example` updated to match.

The old defaults were internally inconsistent — `siglip-base-patch16-224` produces 768 dimensions, not 1152 (1152 is `siglip-so400m`). Nothing depended on the pairing because no real model was ever loaded.

**On not hardcoding 512 twice.** `settings.embedding_dimension` is the model-side contract, and the adapter validates its actual output against it. `ImageModel.embedding` and the migration hold the literal `512` because that is *physical schema* pinned by a migration: it must not silently follow a runtime environment variable away from what the database actually contains. The two are kept honest by `test_image_model_embedding_column_matches_configured_dimension`, which asserts they agree.

---

# 11. Database Migration

Revision **`db526438ced5`**, `down_revision = cbd5647f61b7` (RFC-020). One head, linear chain preserved. No existing migration was edited.

The column is **dropped and re-added**, not altered:

1. drop index `ix_images_embedding_hnsw`
2. drop column `embedding`
3. add `embedding` as `Vector(512)`, nullable
4. recreate `ix_images_embedding_hnsw` with `vector_cosine_ops`

pgvector encodes dimensionality in the column's type modifier, so changing it is a type change, and `ALTER COLUMN ... TYPE vector(512)` has to contend with an index that depends on the column. **No production embeddings exist** — every row written so far carried either `NULL` or a `FakeEmbeddingModel` vector — so there is nothing to preserve and the clean rebuild is both simpler and safer. `downgrade()` reverses it symmetrically back to `Vector(1152)`.

Verified against the running database after applying:

```
 embedding | vector(512) |  | |
Indexes:
    "ix_images_embedding_hnsw" hnsw (embedding vector_cosine_ops)
```

---

# 12. Testing Strategy

| layer | file | count | needs network? |
| --- | --- | --- | --- |
| Adapter unit | `tests/infrastructure/ai/test_clip_embedding_model.py` | 33 | no |
| Translator unit | `tests/infrastructure/ai/test_query_translator.py` | 20 | no |
| Device | `tests/infrastructure/ai/test_device.py` | 8 | no |
| Layer boundaries | `tests/test_ai_layer_boundaries.py` | 9 | no |
| Real models | `tests/infrastructure/ai/test_clip_embedding_model_slow.py` | 26 | **yes, opt-in** |

## 12.1 The `slow` marker

`markers = ["slow: requires downloading and running real AI models"]` is registered in `pyproject.toml` (required — the project runs `--strict-markers`), and `addopts` gained `-m 'not slow'`. A plain `pytest` therefore stays offline and deterministic; `pytest -m slow` opts in.

## 12.2 How the unit tests stay honest

The stubs return **genuine `BaseModelOutputWithPooling` objects holding genuine torch tensors**, deliberately un-normalized, with a 768-wide `last_hidden_state` alongside the 512-wide `pooler_output`. This means the unit tests exercise the adapter's real logic — Pillow opening the file, RGB conversion, field selection, dimension validation, normalization, `EmbeddingVector` construction — without a download. What they cannot verify is that today's contract *is* today's contract; that is what the slow tests are for.

## 12.3 RFC-022 pixel-decoding expectations: activated

RFC-022's `HardCase` carries a dormant `expect_with_pixel_decoding` field. `zero_byte.jpg` and `truncated.jpg` declare `INDEXED` today and `FAILED` once something actually reads pixels — because `FakeEmbeddingModel` hashes the path and never opens the file.

`TestHardCasesWithRealPixelDecoding` activates it: it runs the full production path (`FilesystemImageProvider` → `IndexingWorker` → `IndexOrUpdateImageUseCase` → `ClipEmbeddingModel`) over the generated corpus and asserts that

- the two structurally broken files now fail to index,
- **every other case still indexes**, including the CMYK, grayscale, 16-bit TIFF, RGBA, and EXIF-rotated cases,
- and one broken file still does not abort the run.

No RFC-022 behavior was changed to make this pass. The dataset was already correct.

## 12.4 Error propagation

`encode_image()` does not catch anything. `IndexingWorker.run()` already isolates failures per file, and a second generic `except` would hide which file failed and why. Tests assert that zero-byte files raise `UnidentifiedImageError`, truncated files raise `OSError`, missing files raise `FileNotFoundError`, and that a failure leaves the adapter usable for the next file.

## 12.5 Retrieval quality: still dormant, and why

`queries.json` remains dormant, and `test_relevant_images_rank_above_hard_negatives` remains `xfail`.

RFC-023 removed one of the two blockers — there is now a real semantic model. The other blocker is untouched and belongs to a different RFC: **`SearchImagesUseCase` still encodes the query, discards the embedding, and returns `repository.list()` unranked.** There is no similarity search to measure. Implementing one as a side effect of RFC-023 would be scope creep of exactly the kind this RFC exists to avoid.

The `xfail` reason was rewritten to say this, so the next reader is not misled into thinking a real model was the missing piece.

## 12.6 Existing tests updated for 512

`test_image_model.py`, `test_postgres_image_repository.py`, and `test_sqlalchemy_models_architecture.py` carried literal `1152`s and were updated. `test_alembic_migrations.py` pinned an exact three-revision chain and now expects four, plus new assertions on the RFC-023 revision. `FakeEmbeddingModel` needed no change — it already reads `settings.embedding_dimension`.

---

# 13. Dependencies

Added to `backend/requirements.txt`:

| package | why |
| --- | --- |
| `torch` | Inference runtime for both checkpoints |
| `transformers` | `CLIPModel`/`AutoProcessor`; `AutoTokenizer`/`AutoModelForSeq2SeqLM` |
| `sentencepiece` | Required by `MarianTokenizer` — opus-mt ships a SentencePiece vocabulary |
| `langdetect` | Language detection (section 7.1) |

**`accelerate` was deliberately not added** despite being present in the experiment environment. The adapter loads with a plain `from_pretrained(...)` + `.to(device)` and never uses `device_map` or low-CPU-memory paths, so nothing requires it.

`sacremoses` was also not added. The Marian tokenizer emits a "Recommended: pip install sacremoses" warning without it; translation works correctly regardless, as the bake-off runs themselves demonstrate (they emitted the same warning).

Installed versions used for verification: torch 2.13.0+cpu, transformers 5.15.0, sentencepiece 0.2.2, langdetect 1.0.9.

## 13.1 A mypy note

transformers 5.15.0 ships `py.typed` but its annotations are broken for three calls this adapter makes: `nn.Module.eval` is unannotated, `PreTrainedModel.to` is wrapped in a decorator whose signature mypy reads as taking the model itself, and `PreTrainedModel` does not declare `generate` at all. Four narrowly-scoped `# type: ignore[...]` comments cover exactly these, each with an explanation. `warn_unused_ignores` is on, so they will be flagged the moment a release types them properly.

`langdetect` ships no type information; a scoped `ignore_missing_imports` override covers that one module only.

---

# 14. Performance

On the bake-off machine (CPU only), for the selected checkpoint:

| operation | cost |
| --- | --- |
| CLIP model load | 3.8 s (once per process) |
| Translation model load | 4.8 s (once, and only on the first Portuguese query) |
| Image encode | 0.464 s mean, 0.541 s p95 |
| Text encode | 0.098 s mean |
| PT→EN translation | 0.938 s mean per query |
| **100,000 images, single process** | **12.90 h (0.54 days)** |

Query-path latency for an English query is dominated by text encoding (~0.1 s). A Portuguese query adds ~0.94 s for translation, plus a one-time ~4.8 s on the first such query in a process.

The 12.90-hour figure is single-process. Multi-process indexing is not implemented and is not part of this RFC; note that each worker process would hold its own copy of the model in memory, which is the reason the singleton is per-process rather than per-request.

## 14.1 Against the `ARCHITECTURE.md` §22 targets

| target | measured | verdict |
| --- | --- | --- |
| Index throughput > 1 image/sec on CPU | 0.464 s/img = **2.16 img/s** | ✅ met, ~2× margin |
| Search latency < 1 s (English query) | ~0.098 s text encode | ✅ met, with room for search |
| Search latency < 1 s (Portuguese query) | 0.938 s translate + 0.098 s encode ≈ **1.04 s** | ❌ **already over budget** |
| Startup time < 5 s | models load lazily, not at startup | ✅ met — but see below |

**The Portuguese query path does not meet the documented search-latency target, and it exceeds it before vector search has been implemented at all.** Translation is a query-time cost, unlike the indexing-speed win that justified choosing CLIP in the first place, so the trade-off in section 3.5 is more precisely *English indexing throughput bought at the price of Portuguese query latency*. This is a real, measured problem that the vector-search RFC will inherit; it is recorded here rather than discovered there.

Mitigations exist and are deliberately not implemented in this RFC: caching query embeddings (queries repeat, and encoding is deterministic), batching the translation decode, or moving to a natively multilingual model once throughput allows (section 19, question 1).

On startup: lazy loading keeps process start under target, but the cost is deferred rather than removed. The first request in a process pays ~3.8 s of CLIP load, and the first Portuguese query pays a further ~4.8 s of translation-model load. If first-request latency matters more than start-up latency for a given deployment, a warm-up call at boot is the lever — the `lru_cache` provider makes that a one-liner.

---

# 15. Alternatives Considered

| alternative | why not |
| --- | --- |
| SigLIP2 (`siglip2-base-patch16-384`) | 10.7× slower per image for identical strict-EN and pairwise accuracy (3.5). Wins on native Portuguese; loses decisively on throughput. |
| `jinaai/jina-clip-v2` | 16.0% strict accuracy on this corpus at 48.7 s/image (3.2). Not competitive on either axis. |
| 768-dim CLIP (ViT-L/14) | 16.6× slower for no pairwise gain (3.2). |
| `openai/clip-vit-base-patch32` | Strictly dominated by the LAION checkpoint on every metric (3.1). |
| INT8 dynamic quantization | 1.44× speed for −12/−24/−14 accuracy points and 0.24 mean cosine drift (3.7). |
| ONNX + static quantization | Attempted; `quantize_static()` never completed (onnxruntime#21979). No data. |
| Batching | 1.17× at best batch size — not worth reshaping the port and use cases (3.7). |
| `an aerial photo of {query}` | Worst of three templates despite the domain (3.3). |
| Feeding Portuguese to CLIP directly | 8 points worse than translating first on the selected model (3.4). |
| `pipeline("translation")` | Not reliably registered for this checkpoint in transformers 5.15 (7.2). |
| Language parameter on `encode_text()` | Leaks a model-specific detail into a model-agnostic port (5.1). |
| Normalizing inside `EmbeddingVector` | Imposes one embedding space's convention on every future model (6.1). |
| Eager singleton (RFC-016 pattern) | Would download models on module import (9). |
| Heavyweight language-detection framework | Disproportionate to the problem; no such dependency exists (7.1). |

---

# 16. Risks

1. **Portuguese is measurably weaker than English** — 56.0% vs 60.0% strict, and the gap widens for short queries the detector misses. Accepted, with the SigLIP2 alternative documented for revisiting.
2. **Portuguese query latency already exceeds the `ARCHITECTURE.md` §22 target of < 1 second** — ~1.04 s for translation plus text encoding, before any vector search runs (section 14.1). The most concrete unresolved problem this RFC leaves behind.
3. **Translation quality caps Portuguese accuracy.** `tanque` → `tank` instead of `pond` is a real observed error on a corpus full of ponds.
4. **Single-word queries bypass translation entirely** (7.1). Real users type short queries.
5. **A transformers upgrade could change what `get_image_features()` returns.** This already changed once. The slow tests are the tripwire; the unit-test stubs would not catch it.
6. **Absolute accuracy is modest.** 60.0% strict top-1 on 25 queries against 45 images. The bake-off's own failure lists show consistent weakness on street-level and townscape queries across *every* model tested — likely a property of the corpus and the query phrasing, not just the model. Fine-tuning is the lever.
7. **Small evaluation set.** 45 images, 25 queries. One query is 4 percentage points. Treat differences under ~8 points as noise.
8. **Changing model or dimension requires full re-indexing** and a schema migration (section 18).
9. **The bake-off is not reproducible from the repository** — scripts and README are absent and untracked (1.1). The logs are the only surviving evidence.

---

# 17. Non-goals

Explicitly not delivered by this RFC, and verified absent:

- vector similarity search (`SearchImagesUseCase` is untouched)
- batching of any kind
- fine-tuning, LoRA, or adapters
- quantization
- ONNX or any alternative inference backend
- GPU backends other than CUDA
- multi-process or distributed indexing
- prompt ensembling
- replacing `FakeEmbeddingModel` (it remains the test double)

---

# 18. Fine-tuning and Future Work

The chosen checkpoint is a **starting point, not an architectural commitment to CLIP**. Future work may include domain-specific fine-tuning on aerial property imagery, LoRA or adapter layers, a different CLIP checkpoint, SigLIP or another multilingual model, or an optimized inference backend.

All of these are replaceable behind `EmbeddingModelPort`. None requires a Domain or Application change. That is the entire point of section 5.

**The one consequence that must not be forgotten:** changing the embedding model — or merely its dimension — invalidates every stored vector. It requires

1. a new Alembic migration if the dimension changes,
2. a full re-index of every image,
3. an HNSW index rebuild.

This is expected and normal for embedding systems. It must not leak into Domain or Application, and it does not. A migration strategy for it is deliberately not designed here; it belongs to whichever RFC actually swaps the model.

Also deferred: **text-embedding caching**. Query embeddings are deterministic and text batching showed a 2.7× gain — but neither matters until vector search exists to make query latency visible.

---

# 19. Open Questions

1. Should Portuguese support move to a natively multilingual model once throughput is solved (GPU, batching, or multi-process)? SigLIP2's 64.0% native strict-PT is the benchmark to beat.
2. Is the ~60% strict top-1 ceiling a model limitation or a corpus/query-phrasing artifact? Every model tested failed the same street-level and townscape queries, which points at the latter.
3. Should the bake-off scripts be committed so the numbers in section 3 are reproducible? They are currently absent and untracked.
4. What is the right short-query strategy — a curated PT term list, a better detector, or accepting the gap?

---

# 20. Deliverables

**New:**

- `backend/app/infrastructure/ai/clip_embedding_model.py`
- `backend/app/infrastructure/ai/query_translator.py`
- `backend/app/infrastructure/ai/device.py`
- `backend/alembic/versions/db526438ced5_change_embedding_to_512_dimensions.py`
- `backend/tests/infrastructure/ai/test_clip_embedding_model.py`
- `backend/tests/infrastructure/ai/test_clip_embedding_model_slow.py`
- `backend/tests/infrastructure/ai/test_query_translator.py`
- `backend/tests/infrastructure/ai/test_device.py`
- `backend/tests/test_ai_layer_boundaries.py`
- `docs/rfcs/rfc-023-clip-embedding-adapter.md`

**Modified:**

- `backend/app/infrastructure/ai/__init__.py` — documents why the adapter is not re-exported
- `backend/app/infrastructure/config/settings.py` — model, dimension, device defaults
- `backend/app/infrastructure/database/models/image_model.py` — `Vector(512)`
- `backend/app/domain/value_objects/index_metadata.py` — docstring made dimension-agnostic
- `backend/app/presentation/dependencies/__init__.py` — lazy CLIP singleton
- `backend/requirements.txt` — torch, transformers, sentencepiece, langdetect
- `pyproject.toml` — `slow` marker, `-m 'not slow'`, langdetect mypy override
- `.env.example` — new defaults
- `backend/tests/dataset/test_queries.py` — dormancy re-explained
- `backend/tests/infrastructure/test_alembic_migrations.py` — four-revision chain + RFC-023 assertions
- `backend/tests/infrastructure/test_image_model.py`, `test_sqlalchemy_models_architecture.py`, `tests/infrastructure/persistence/test_postgres_image_repository.py` — 1152 → 512
- `backend/tests/presentation/test_dependencies.py` — CLIP wiring, laziness, no-download

**Unchanged, deliberately:** `EmbeddingModelPort`, `EmbeddingVector`, `FakeEmbeddingModel`, `SearchImagesUseCase`, `IndexImageUseCase`, `IndexOrUpdateImageUseCase`, `IndexingWorker`, and every RFC-022 dataset file.

---

# 21. Validation

| check | result |
| --- | --- |
| `pytest` | 297 passed, 26 deselected, 1 xfailed |
| `pytest -m slow` | 26 passed |
| `pytest` with `HF_HUB_OFFLINE=1` + empty `HF_HOME` | 297 passed — no downloads |
| `black --check .` | clean |
| `ruff check .` | clean |
| `mypy` | 5 errors, all pre-existing and unrelated (baseline confirmed by stashing this branch) |
| `alembic heads` | `db526438ced5 (head)` — exactly one |
| `alembic current` | `db526438ced5 (head)` |
| schema | `embedding vector(512)` |
| index | `ix_images_embedding_hnsw hnsw (embedding vector_cosine_ops)` |
| residual test rows | `SELECT count(*) FROM images` → 0 |
