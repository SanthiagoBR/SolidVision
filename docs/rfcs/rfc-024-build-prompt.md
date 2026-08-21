# RFC-024 — Embedding Pipeline: Implementation Prompt

Implement the RFC-024 Embedding Pipeline for SolidVision in `backend/app/`.

This document is committed deliberately. The RFC-023 build prompt lived in
`experiments/rfc-023-siglip-bakeoff/`, was never tracked by git, and no longer
exists — along with every bake-off script it referred to. Keep this file in
`docs/rfcs/`.

Before changing code, inspect the current repository state, `ARCHITECTURE.md`,
`AI_Context.md`, `docs/rfcs/rfc-022-demo-dataset.md`,
`docs/rfcs/rfc-023-clip-embedding-adapter.md`, the existing tests, the Alembic
head, and the database schema. Preserve the project's current architectural
boundaries.

---

## 0. Where the pipeline stands today

Verified against the repository at commit `a277efc` (RFC-023). Re-verify before
relying on any of it.

| Concern | State |
| --- | --- |
| Discovery | `FilesystemImageProvider.discover()` — `sorted(root.rglob("*"))`, yields `DiscoveredImageFile` |
| Orchestration | `IndexingWorker.run()` — one file at a time, per-file `try/except` |
| Incremental skip | `IndexOrUpdateImageUseCase` — compares `file_size` + `file_modified_at` only |
| Embedding | `ClipEmbeddingModel` (RFC-023) — `encode_image(image)`, one image per call |
| Persistence | `PostgresImageRepository.save_indexed()` — `session.get()` + `session.commit()` **per image** |
| Metadata read | `get_index_metadata()` — another `session.get()` **per image** |
| Alembic head | `db526438ced5` |
| Schema | `images(id, path, filename, extension, file_size, file_modified_at, embedding vector(512))` |

Measured (RFC-023 §14): CLIP inference **0.464 s/image**, giving 2.16 img/s and
~12.9 h for 100,000 images single-process. A local Postgres round trip is
single-digit milliseconds. **The pipeline is therefore roughly 99%
inference-bound.** Every optimization proposal in this document must be judged
against that fact.

Settings defined but never read by any code: `batch_size`, `worker_count`,
`top_k_results`, `minimum_similarity`, `default_collection_name`.

---

## 1. Objective

Transform the indexer from sequential per-image processing into a pipeline that
is **incremental, batched where measurement justifies it, observable, and
resilient**, without sacrificing per-image error isolation and without leaking
model or batching concerns into Domain or Application.

---

## 2. Non-goals

Explicitly out of scope. Do not implement any of these, and verify at the end
that none crept in:

- vector similarity search / ranking / top-K (that is RFC-025)
- `Collections` table, `Collection` entity, `collection_id` foreign keys
- thumbnail generation (`thumbnail_path`, `THUMBNAIL_SIZE`)
- `width` / `height` extraction
- fine-tuning, LoRA, adapters
- quantization, ONNX, alternative inference backends
- multi-process or distributed workers (`worker_count` stays unused)
- GPU backends beyond the CUDA auto-detection RFC-023 already added
- replacing or modifying `FakeEmbeddingModel`'s role as the test double
- the full 100,000-image benchmark run (that is RFC-026)

**Batching and parallelism are two different levers on the same bottleneck,
and this RFC pulls only one of them.** The pipeline is CPU-bound on a single
process (§0). Batching amortizes fixed per-call overhead within one process;
multiple worker processes would let CPU-bound inference actually run
concurrently, which batching alone cannot provide. That is a materially
different, and possibly larger, source of throughput — but it is unmeasured,
and naively spawning processes around a PyTorch CPU workload can just as
easily saturate RAM and contend for the same CPU cores, making things worse
rather than better. It needs its own benchmark and its own RFC, not an
addition bolted onto this one. Do not let this section's exclusion of
multi-process workers be read as "parallelism doesn't matter" — it is "not
measured yet, so not this RFC."

---

## 3. Benchmark first — this gate has already been measured

**The gate is measured and passed. Batching is in scope.** Reproduce these
numbers as part of the RFC's own benchmark work (§3.1 below still applies in
full — do not skip re-running it just because a prior measurement exists) but
do not re-litigate whether to build section 5; that question is closed.

RFC-023's `batching_check_output.log` measured 1.17× at batch size 32 — but
that run used `google/siglip2-base-patch16-384`, **not** the CLIP checkpoint
that shipped. That number was never valid evidence for this decision.

CLIP B-32 batch scaling has since been measured directly (`laion/CLIP-ViT-B-32-laion2B-s34B-b79K`,
45-image demo corpus, CPU, `clip_batching_check_results.json`):

| batch size | s/image | speedup vs 1 |
| --- | --- | --- |
| 1 | 0.4697 | 1.00× |
| 2 | 0.4203 | 1.12× |
| 4 | 0.3790 | 1.24× |
| **8** | **0.3363** | **1.40×** |
| 16 | 0.3295 | 1.43× |
| 32 | 0.3192 | 1.47× |
| 64 | 0.3417 | 1.37× (regression vs 32 — cache pressure, not noise-free) |
| 128 | 0.3171 | 1.48× |

Embedding drift versus batch-of-1 stayed at ~1.0000 mean/min across every
batch size — batching does not change what is computed, only how, satisfying
the equivalence requirement below.

**Read the curve, not just the peak.** It plateaus hard after 8: batch 8
alone captures 87% of the total speedup measured out to 128
((1.40−1)/(1.48−1) ≈ 0.87), while every step past it buys single-digit
percentage points for a linear increase in the memory this batch must hold
decoded (§9). 64 actively regressing below 32 is a signal that "bigger batch
is always better" does not hold on CPU past some point — do not assume
monotonicity when choosing a default.

**Gate outcome: 1.40× at batch 8 clears this RFC's proposed 1.3× threshold.**
Implement section 5. Treat 1.3× as an initial acceptance bar, not a universal
law — a number alone should not decide a Domain contract change. What makes
this an easy call is that 1.40× is comfortably clear of 1.3× *and* the
contract options in §5 are all modest in complexity; had the result landed
at, say, 1.28× against the most invasive contract option, that would be
grounds to revisit the threshold or pick a cheaper option, not to treat 1.3×
as sacred. Document the reasoning, not just the number, when the RFC records
this decision.

Default `batch_size` to **8**, not 32 — the plateau shape means 32 buys
almost nothing over 8 in speed while quadrupling the decoded-image memory
footprint that §9 flags as the real risk on production-sized (e.g. 24 MP)
photos. This is a default, not a hard cap: keep the setting configurable
(§14) so it can be raised on a machine with memory to spare, but justify 8
as the shipped default in the RFC using this curve.

### 3.1 Still required: your own benchmark run, plus the memory axis

The table above has no memory column — it is speed-only, from an external
measurement, not something this implementation produced itself. Build the
benchmark described below regardless, because:

- the numbers must be reproducible from inside this repository, not taken on
  faith from a file outside version control history at implementation time
- **peak RSS per batch size is still completely unmeasured** — the speed
  plateau does not tell you where the memory cliff is, and §9's 2.3 GB
  estimate for a batch of 32 real drone photos is currently a calculation,
  not a measurement

`ARCHITECTURE.md` §22 reserves `scripts/` for benchmark scripts. Create a
benchmark that reports, for the real CLIP adapter on the real demo corpus:

| batch size | total s | s/image | img/s | speedup vs 1 | peak RSS MB | est. time for 100k images |
| --- | --- | --- | --- | --- | --- | --- |
| 1, 2, 4, 8, 16, 32, 64 | | | | | | |

The last column is the one that actually matters to the project. A speedup
multiplier is abstract; `ARCHITECTURE.md` §22's target is concrete — "index
100,000 images" — and the RFC should let a reader answer "how long does that
actually take at this batch size?" without doing the arithmetic themselves.
RFC-023 §3.1/§14 already reports numbers this way (hours/days to 100k per
model); follow that convention here rather than stopping at a bare multiplier.

It must also verify **embedding equivalence**: batched embeddings must match
batch-of-1 embeddings to within floating-point tolerance. Batching must change
only *how* the work is done, never *what is computed*.

Record the measured numbers — including this run's own speed figures, which
should corroborate the table above — in the RFC. If peak RSS at batch 8 is
already uncomfortably high on demo-corpus-sized images, say so and revisit the
default before shipping it.

---

## 4. SHA256 content hashing

`ARCHITECTURE.md` §16 specifies a three-step, cost-ascending incremental check.
RFC-020 implemented steps 1 and 2 only. Step 3 is missing:

1. File exists in the database?
2. `file_modified_at` or `file_size` changed?
3. **If step 2 says "possibly changed", compute SHA-256 to confirm the bytes
   actually changed.**

Today any mtime touch — a copy, a backup restore, a `git checkout` — forces a
full re-embed. **Each avoided re-embed saves 0.464 s**, which is a larger unit
win than anything batching offers.

Implement:

- a new nullable `content_hash` column on `images` (see section 15)
- `IndexMetadata` and `IndexingRecord` carry the hash
- `IndexOrUpdateImageUseCase` gains step 3: hash **only** when size/mtime
  already differ; if the hash matches what is stored, update the filesystem
  metadata and skip the embedding
- hashing must stream the file in chunks, never `read()` it whole

### Architectural requirement

Hashing is filesystem I/O, so it cannot live in the Application layer. Introduce
a port — for example `ContentHasherPort` in `app/domain/services/` — mirroring
how `EmbeddingModelPort` works, with the real implementation in
`app/infrastructure/filesystem/`. The Application layer must depend only on the
port.

### Hard constraint

RFC-022 §7.1 makes `ImageId` **path**-derived on purpose, so that
byte-identical twins produce two rows. `content_hash` is a change-detection
field only. It must never influence identity, deduplication, or row lookup. If
`duplicate_content_a.jpg` and `duplicate_content_b.jpg` stop producing two
distinct rows, the change is wrong — there is an existing test pinning this.

---

## 5. Batch inference — in scope; the gate passed at 1.40× (batch 8)

Section 3 measured this on the real CLIP checkpoint and cleared the 1.3×
threshold. Build it. The open question is no longer *whether*, only *how the
contract should look* and *what default batch size ships*.

Default `batch_size` to **8** (§3), not 32 — justify this in the RFC using the
plateau curve, and confirm it still holds once §3.1's memory measurement
exists. If the memory benchmark shows batch 8 is itself expensive on
realistic photo sizes, that is grounds to lower the default further, but it
is not grounds to abandon batching — even batch 2 (1.12×) already provides
positive, equivalence-preserving speedup.

### The contract question

`EmbeddingModelPort` currently exposes:

```python
encode_image(self, image: Image) -> EmbeddingVector
encode_text(self, text: str) -> EmbeddingVector
```

Adding batch capability is a **Domain contract change**. Evaluate these options
in the RFC and justify the choice:

1. **Add `encode_images(images) -> list[EmbeddingVector]` to the port.**
   Honest and explicit, but every implementation — including
   `FakeEmbeddingModel` — must satisfy it, and it puts a performance concern
   into a Domain contract.
2. **Keep the port single-image; batch internally inside the adapter.** Domain
   stays clean, but the adapter cannot know when a batch is complete without an
   explicit flush, which is awkward and hides latency.
3. **A separate optional capability**, e.g. a `BatchEmbeddingModelPort` that
   implementations may also satisfy, with the pipeline falling back to
   single-image when it is absent.

Whatever is chosen: the Application layer must still work correctly with an
implementation that only supports single images, and `FakeEmbeddingModel` must
remain usable without becoming a second implementation of batching logic.

`settings.batch_size` already exists (default 16) and is unused. Either reuse
it — updating its default to 8 per §3's measurement — or introduce a
clearly-named dedicated setting; do not add a second overlapping knob without
saying why, and do not leave a stale default of 16 or 32 sitting uncorrected
next to a decision that measured 8 as the better trade-off.

### Where batching actually happens — a decision the RFC must state explicitly

Verified in the current code: `IndexOrUpdateImageUseCase.execute(image, ...)`
is a single-image method. It does the skip-check (via
`get_index_metadata()`), the embed (via `encode_image()`), and the persist
(via `save_indexed()`) for exactly one image, in one call. `IndexingWorker`
calls it once per discovered file.

That signature cannot batch. Calling `execute()` N times in a loop to "fill a
batch" does not make inference batched — it is still N sequential single-image
calls with extra bookkeeping around them. Something has to change shape.
**Do not begin section 5 or 6's implementation until the RFC states, in
writing, which of the following happens — and produces a short table of
"component → responsibility after this RFC" so the answer is checkable rather
than implicit in a diff:**

1. **Decompose `IndexOrUpdateImageUseCase`** into separable steps — a skip
   decision, an embed step, a persist step — with a new orchestrating
   component (in `IndexingWorker`, or a new Application-layer coordinator)
   that runs the skip decision over every discovered file, collects the
   surviving candidates into batches, calls the batch-capable port method
   once per batch, then persists. `IndexOrUpdateImageUseCase` may be kept
   as a thin single-image entry point built from the same decomposed steps
   (useful for a future single-file reindex path), retired, or reduced to
   just the skip-decision responsibility — the RFC must say which.
2. Any alternative design that achieves the same batching without this
   decomposition, if one exists — but it must be stated and justified, not
   discovered mid-implementation.

Whatever is chosen, `IndexingWorker` must remain the orchestrator per §10 —
it stays dependency-injected and does not acquire its own model, provider, or
session — and per-file error isolation (§6) must still hold after the
decomposition, not just before it.

---

## 6. Error isolation under batching

**This is the most important correctness requirement in the RFC.**

RFC-021 gave `IndexingWorker.run()` a per-file `try/except` so one corrupt file
cannot abort a run. RFC-023 relied on that when it deliberately let Pillow and
inference errors propagate out of the adapter. **Batching destroys that
property**: a batch of 32 containing one truncated JPEG fails as a unit, and the
naive implementation loses 31 good images and cannot say which file was at
fault.

Required strategy:

```
batch of N
    ↓ fails
retry the same N individually
    ↓
N-1 succeed, 1 is logged as failed with its real exception and path
```

This preserves throughput in the common case (batches succeed) and restores
exact per-file isolation in the failure case. The individual retry must surface
the original exception and the offending path, not a generic wrapper.

Do not "fix" this by adding a broad `try/except` inside `ClipEmbeddingModel`.
RFC-023 §4 deliberately keeps that adapter free of error swallowing, and that
decision stands.

The RFC-022 hard-case corpus already contains `zero_byte.jpg` and
`truncated.jpg` with `expect_with_pixel_decoding=FAILED`. The batched pipeline
must produce exactly the same outcome as the sequential one for every case in
that corpus. That is the acceptance test.

---

## 7. Persistence: metadata prefetch is mandatory, bulk writes are conditional

These two used to be presented as one block. They are not the same kind of
change and do not share a justification — split them.

`save_indexed()` currently issues a `session.get()` plus a `session.commit()`
for **every image**, and `get_index_metadata()` issues another `session.get()`.
That is two SELECTs and one fsync-ing commit per file.

### 7.1 Bulk metadata prefetch — mandatory

This is a Big-O fix, not a throughput optimization, and the two must not be
conflated. On a re-index where most files are unchanged — the common case at
scale — the skip path currently issues one `get_index_metadata()` SELECT per
discovered file. At 100,000 images with 80,000 unchanged, that is 80,000
individual round trips **before inference ever runs**, regardless of whether
inference itself is batched. Implement a single query returning
`IndexMetadata` for many `ImageId`s at once, and have the skip decision (§8)
consult it instead of querying per file.

### 7.2 Bulk upsert writes — conditional, gate it the same way section 3 gates inference batching

Be honest in the RFC about the payoff: at 0.464 s/image of inference, the
database write path is roughly 1% of runtime. **Do not implement bulk upsert
writes, one-transaction-per-batch semantics, and a bulk-write fallback path
as a foregone conclusion.** That is real, independent complexity — a second
batching dimension layered on top of inference batching — for a return this
RFC's own numbers say is small.

Measure first, the same discipline section 3 already applies to inference:

1. Implement inference batching (§5) and the mandatory prefetch (§7.1) alone.
2. Benchmark the pipeline end to end. If Postgres round trips are still a
   visible fraction of total time — say, more than ~5% — bulk upsert writes
   are justified; implement them.
3. If the database has genuinely disappeared into the noise once §7.1 alone
   is in place, **do not implement bulk upsert writes in this RFC.** Record
   the measurement showing why, and leave it as explicitly-noted future work.

If step 2 does justify implementing it, the design still stands as follows —
non-negotiable if built at all:

- a bulk upsert path added to `ImageRepository` as a first-class port method
  (never bypass the repository — `AI_Context.md` forbids querying the
  database from use cases)
- **one transaction per persistence batch**, with this contradiction resolved
  explicitly: "one transaction per batch" and "isolate errors per file"
  conflict, since a strict transactional batch of 32 rolls back all 32 when
  row 17 violates a constraint, discarding 31 embeddings that cost ~15
  seconds of CPU to produce. Resolve it the same way as section 6: **attempt
  the bulk write; on failure, fall back to per-row writes within the
  batch**, so only the genuinely bad row is lost.
- the inference batch size and the persistence batch size are **not required
  to be equal** — decide and document whether they are separate settings;
  coupling them without stating why is not acceptable.

---

## 8. Incremental skip must precede batch assembly

The model must never receive an image that already has a valid embedding.

```
1000 discovered
    ↓
800 unchanged  → skipped before any batch is formed
    ↓
200 to process → batches of N
```

Batches are assembled **after** the skip decision, from surviving candidates
only. A batch must never be padded with already-indexed images. Verify this with
a test that counts adapter invocations on a second run over an unchanged corpus:
the count must be zero.

---

## 9. Memory ceiling

Batching holds N decoded images in memory simultaneously. The RFC must specify
and enforce a decode strategy:

```
open → decode → convert to RGB → preprocess to the model's input size
     → discard the full-size decoded image → accumulate only the small tensor
```

Never accumulate full-size decoded images across a batch.

The numbers that make this real:

- the RFC-022 demo corpus is 1024×768, ~1.8 MB decoded — a batch of 32 is
  ~57 MB, harmless
- a 24-megapixel drone or phone photo decodes to ~72 MB — **a batch of 32 is
  ~2.3 GB**

**The demo corpus cannot expose this bug.** A test using only the demo corpus
will pass while the production path exhausts memory on a user's real photos. Add
a test that generates a small number of deliberately large synthetic images and
asserts that peak memory stays bounded as batch size grows — or, if measuring
RSS portably proves unreliable, assert the structural property instead (that no
full-size decoded image is retained past preprocessing) and document why the
weaker assertion was chosen.

The benchmark in section 3 must report peak memory per batch size.

---

## 10. Pipeline stages

Make the stages explicit rather than implicit in one loop:

```
DISCOVERY → SKIP DECISION → PREPROCESSING → BATCH INFERENCE → PERSISTENCE
```

The goal is that each stage can be measured and optimized independently. Do
**not** over-engineer this: no queues, no threads, no async, no producer/
consumer machinery in this RFC. Streaming/generator composition is sufficient
and keeps memory bounded.

`IndexingWorker` remains the orchestrator. It must stay a dependency-injected
component that never constructs its own provider, use case, model, or session.

**Load-bearing detail:** `FilesystemImageProvider.discover()` sorts its results.
That determinism is what makes reproducible batching and any future path
checkpoint meaningful. Do not remove the sort; state in the RFC that it is
relied upon.

---

## 11. Metrics and observability

A run must end with a machine-readable and human-readable summary. Target shape:

```
Indexing finished

Discovered:      10000
Skipped:          8000   (unchanged)
Skipped (hash):    150   (mtime changed, content identical)
Indexed:          1800
Failed:             50

Inference:
  images:         1800
  batches:          57
  avg batch:     1.20s
  throughput:    26.4 img/s

Persistence:
  batches:          18
  avg write:     0.08s
  fallbacks:         1   (bulk failed, degraded to per-row)

Total:
  elapsed:       92.4s
  throughput:    21.1 img/s
```

`Skipped (hash)` is the number that proves section 4 is earning its keep — it
counts files that would have been re-embedded before this RFC.

Counters belong to the pipeline as returned/logged data. Use the existing
`app.infrastructure.logging` factory; do not introduce a second logging
mechanism. Do not add a metrics/telemetry dependency.

---

## 12. Worker CLI entry point

`AI_Context.md` promises:

```
python -m infrastructure.workers.indexing_worker
```

This does not exist. During RFC-023 validation a throwaway script had to be
written by hand to run the pipeline at all.

Create a real entry point. It is a composition root: it wires
`FilesystemImageProvider`, the use case, the repository, and the embedding model
together and runs the worker. Reuse `app.presentation.dependencies` where it
already provides what is needed rather than building a parallel wiring
mechanism — `AI_Context.md` forbids a second DI mechanism.

Note the path discrepancy: the promised module path is
`infrastructure.workers.indexing_worker` but the package is
`app.infrastructure.workers.indexing_worker`. Resolve this and say which is
correct in the RFC.

The CLI must accept an explicit root argument and must **not** silently default
to a path that could point at a user's real photo collection. Follow the
reasoning already documented in `dataset_tools/seed_demo.py`'s module docstring
about why defaulting to `indexing_root_path` is dangerous.

---

## 13. Resumability — deliberately deprioritized

`ARCHITECTURE.md` §15 defines an `IndexingJobs` table with `last_processed_path`
as a checkpoint, and `AI_Context.md` calls for resumable jobs.

**Do not implement this in RFC-024 unless everything else is complete**, and
justify the decision in the RFC with this reasoning: RFC-020's incremental skip
already provides crash recovery. Re-running after an interruption skips
everything already indexed. With the bulk metadata prefetch from section 7, that
re-scan is cheap. `last_processed_path` is therefore a restart *optimization*,
not a correctness requirement — and the `IndexingJobs` table as specified has a
`collection_id` foreign key to a `Collections` table that does not exist and is
out of scope (section 2).

If you disagree after inspecting the code, say so and explain, rather than
silently implementing it.

---

## 14. Configuration

Use the existing `pydantic-settings` mechanism in
`backend/app/infrastructure/config/settings.py`. Never read `os.environ`
directly outside that package.

- decide the fate of the existing unused `batch_size` (default 16)
- add a persistence batch size if section 7 concludes they should differ
- update `.env.example` for anything added
- do not hardcode batch sizes in multiple places

---

## 15. Database migration

Create one new Alembic migration. Do **not** edit an already-applied migration.

The current head is `db526438ced5` (RFC-023). Verify this before writing
anything.

Required change: add a nullable `content_hash` column to `images`, sized for a
hex-encoded SHA-256 digest, with a correct `downgrade()`.

Do not touch the `embedding` column, the HNSW index, or any other existing
schema. There is an existing test asserting that the RFC-023 migration was not
rewritten; add an equivalent assertion for this one.

Existing rows will have `content_hash = NULL`. The use case must treat a NULL
stored hash as "unknown, cannot confirm unchanged" and fall through to
re-embedding rather than assuming a match. Backfilling is not required.

After applying, verify the real schema with `psql` and record the output in the
RFC.

---

## 16. Architectural boundaries that must not break

These are enforced by `backend/tests/test_ai_layer_boundaries.py`. Run it.

- Domain and Application must import no `torch`, `transformers`, `langdetect`,
  `PIL`, or `numpy`, and no module path containing `clip`/`siglip`/`huggingface`
- `EmbeddingModelPort` must remain free of implementation vocabulary
- batching must not make the Domain aware of CLIP, tensors, or devices
- use cases must never query the database directly — everything through
  `ImageRepository`
- `IndexingWorker` must not construct its own dependencies
- `FakeEmbeddingModel` must remain a working test double, and the fast test
  suite must remain runnable without downloading any model

If section 5 adds a batch method to the Domain port, extend the boundary test to
cover it.

---

## 17. Testing strategy

Follow the conventions already in the repository.

- markers: `slow` is registered in `pyproject.toml` and deselected by default
  via `addopts = "-ra --strict-markers -m 'not slow'"`. Anything touching real
  Hugging Face models must be marked `slow`.
- the default `pytest` run must remain offline and deterministic. It currently
  passes with `HF_HUB_OFFLINE=1` and an empty `HF_HOME`; it must still do so.
- database tests use the SAVEPOINT-isolated `db_session` fixture in
  `backend/tests/conftest.py` and must leave no rows behind.

At minimum, test:

**Hashing** — hash computed only when size/mtime differ; identical content with
a changed mtime skips the embedding; genuinely changed content re-embeds; a NULL
stored hash forces re-embedding; hashing streams rather than loading whole
files; byte-identical twins still produce two distinct rows.

**Batching (if implemented)** — batched and sequential runs produce equivalent
embeddings; a batch containing one corrupt file still indexes the other N-1; the
failed file is reported with its real path and exception; the corpus-wide
outcome matches the RFC-022 hard-case expectations exactly; batches are
assembled only from non-skipped candidates; a second run over an unchanged
corpus invokes the model zero times.

**Persistence** — bulk upsert writes all rows; a failing row degrades to
per-row and the other rows survive; the bulk metadata prefetch returns the same
results as N individual `get_index_metadata()` calls.

**Memory** — peak usage stays bounded as batch size grows on deliberately large
synthetic images (see section 9).

**Metrics** — counters are accurate against a corpus with a known mix of new,
unchanged, hash-identical, and corrupt files.

**CLI** — the entry point composes the real pipeline and refuses to run without
an explicit root.

---

## 18. Validation before declaring RFC-024 complete

Run the project's actual configured commands:

```
pytest
pytest -m slow
black --check .
ruff check .
mypy
alembic heads
alembic history
```

Confirm:

- `mypy` reports **only the 5 pre-existing errors** — that is the established
  baseline (in `test_embedding_model_port.py`, `test_image_repository_port.py`,
  `test_postgres_image_repository.py` ×2, `test_health.py`). Verify the baseline
  by stashing your changes if unsure. Do not "fix" unrelated pre-existing errors.
- exactly one Alembic head, chain still linear, no migration rewritten
- `content_hash` exists in the real schema; `embedding` is still `vector(512)`
  and `ix_images_embedding_hnsw` still uses `vector_cosine_ops`
- no residual rows left in the development database
- the default test suite downloads no Hugging Face models
- Domain and Application remain free of AI-library imports
- no vector search, no `Collections` table, no thumbnails, no fine-tuning, and
  no quantization were introduced

Report every changed file and why it changed. If any requirement cannot be
satisfied, stop and report it rather than silently reducing scope.

---

## 19. RFC document

Create `docs/rfcs/rfc-024-embedding-pipeline.md`, following the organization and
level of detail of `docs/rfcs/rfc-023-clip-embedding-adapter.md`.

It must document: context; problem; **the benchmark results and the gate
decision they produced**; the SHA-256 incremental-check design and the
identity constraint it must not violate; the batching contract decision and its
alternatives; the error-isolation strategy; the persistence-batching design and
its transactional/isolation resolution; the memory strategy; pipeline stages;
metrics; the CLI entry point; configuration; the migration; testing strategy;
measured performance against `ARCHITECTURE.md` §22; alternatives considered;
risks; non-goals; and future work.

Cite real measured numbers. Do not invent benchmark results. If something was
not measured, say so explicitly.

Record honestly which proposals were **rejected by measurement** — a rejected
optimization with a number attached is more valuable to the next reader than a
silently dropped idea.

---

## 20. Important implementation rule

Do not blindly trust this prompt over the repository.

This document describes intent. The repository is the source of truth for
existing interfaces, naming conventions, dependency versions, the Alembic head,
test conventions, dependency injection, filesystem behavior, and database
configuration.

Before modifying anything, inspect: the current `EmbeddingModelPort`,
`IndexOrUpdateImageUseCase`, `IndexingWorker`, `PostgresImageRepository`,
`ImageRepository`, `IndexMetadata`, `IndexingRecord`, `DiscoveredImageFile`, the
RFC-022 hard-case generator and its tests, the RFC-023 adapter, and the current
migration chain.

Then implement the smallest coherent change that satisfies this RFC.

Do not create speculative abstractions. Do not modify unrelated files. Do not
rewrite previous migrations. Do not implement future RFCs.

At the end, provide a concise implementation report listing: files changed; the
migration created; tests added or changed; validation results; architectural
decisions; **the benchmark numbers and what they caused you to build or skip**;
and any deviations from this prompt with the reason.
