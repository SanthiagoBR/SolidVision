# RFC-024 — Embedding Pipeline

**Status:** Implemented
**Depends on:** RFC-020 (incremental metadata), RFC-021 (indexing worker), RFC-022 (demo dataset), RFC-023 (CLIP adapter)
**Migration:** `26058b9e1d9a_add_image_content_hash`
**Benchmark:** `scripts/benchmark_indexing.py`

---

# 1. Context

RFC-021 gave SolidVision an indexing worker. RFC-023 gave it a real embedding
model. Between them the pipeline worked, and it worked one image at a time:

```
discover -> build entity -> SELECT metadata -> encode -> SELECT row -> INSERT -> COMMIT
```

for every file, in sequence, with a `try/except` around each iteration.

That shape is correct and it does not scale. `ARCHITECTURE.md` §22 sets a
target of 100,000+ indexed images, and RFC-023 §14 measured the sequential
cost at 0.464 s/image — 12.90 hours for that collection, before any of the
per-file database work is counted.

Three things in the existing pipeline were doing avoidable work:

- **Every changed mtime forced a full re-embed.** `ARCHITECTURE.md` §16
  specifies a three-step, cost-ascending incremental check. RFC-020 built
  steps 1 and 2 (existence, then size/mtime) and stopped there, so a copy, a
  backup restore, or a `git checkout` re-embedded an entire collection whose
  bytes had not changed.
- **The model was called once per image.** Whether that mattered was
  unmeasured on the checkpoint that actually shipped.
- **The database was queried once per image, twice.** On a re-index where
  most files are unchanged — the common case at scale — that is one round
  trip per discovered file before inference ever runs.

There was also no way to run the pipeline at all. `AI_Context.md` promised
`python -m infrastructure.workers.indexing_worker`; no such entry point
existed, and RFC-023's validation needed a hand-written throwaway script.

# 2. Problem

Turn the indexer into a pipeline that is **incremental, batched where
measurement justifies it, observable, and resilient** — without losing
per-image error isolation, and without leaking model or batching concerns
into Domain or Application.

The tension that shapes the whole RFC: **batching and error isolation pull in
opposite directions.** A batch of 8 containing one truncated JPEG fails as a
unit. The naive implementation loses 7 good images and cannot say which file
was at fault. Any batching design that does not answer this is not
acceptable, however fast it is.

---

# 3. Benchmark

`scripts/benchmark_indexing.py` (new; `ARCHITECTURE.md` §22 reserves
`scripts/` for exactly this) measures the real `ClipEmbeddingModel` against
the real RFC-022 demo corpus. All numbers below are from this repository, on
this machine, on CPU. Nothing is inherited.

**Machine:** Windows 10, CPU-only inference, PostgreSQL 17 + pgvector in
Docker on the same host.
**Corpus:** 45 committed demo photos, ~1024×768.
**Checkpoint:** `laion/CLIP-ViT-B-32-laion2B-s34B-b79K`.

## 3.1 Batch-size sweep

| batch size | total s | s/image | img/s | speedup vs 1 | peak RSS MB | RSS growth MB | est. h for 100k |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 21.53 | 0.4785 | 2.09 | 1.00× | 732.2 | 14.4 | 13.29 |
| 2 | 18.15 | 0.4033 | 2.48 | 1.19× | 733.8 | 12.4 | 11.20 |
| 4 | 17.63 | 0.3918 | 2.55 | 1.22× | 737.2 | 14.5 | 10.88 |
| **8** | **16.51** | **0.3670** | **2.73** | **1.30×** | **747.0** | **22.7** | **10.19** |
| 16 | 15.59 | 0.3463 | 2.89 | 1.38× | 770.7 | 41.5 | 9.62 |
| 32 | 15.63 | 0.3473 | 2.88 | 1.38× | 817.0 | 76.3 | 9.65 |
| 64 | 15.51 | 0.3447 | 2.90 | 1.39× | 845.2 | 94.1 | 9.57 |

The last column is inference only, and is the column that answers the
question `ARCHITECTURE.md` §22 actually asks. The batch-of-1 figure (13.29 h)
corroborates RFC-023 §14's independently measured 12.90 h.

Three runs were taken. Batch 8 measured 1.30×, 1.29×, and 1.31×; batch 32
measured 1.38×, 1.40×, and 1.46×. Run-to-run spread is a few percent, so
differences below ~5% in this table should not be read as real.

## 3.2 Memory — the axis that was previously unmeasured

The "RSS growth" column is peak resident memory during the sweep minus
settled RSS before it, sampled on a background thread at 20 ms (checking
between batches would miss the peak entirely, since the interesting
allocation exists only during a forward pass).

Growth is **linear in batch count and independent of source resolution**,
which is the memory strategy in §9 working as designed. From batch 8 upward
it runs at roughly 1.2–1.5 MB per image in flight. That is the preprocessed
input tensor (224×224×3 float32 ≈ 602 KB) plus transient activations — *not*
the decoded photo.

This is the number that matters for the risk §9 raises. A batch of 32
24-megapixel photos does **not** cost 2.3 GB, because only one full-size
decode is ever alive: the batch holds 32 fixed-size tensors (~19 MB) plus one
72 MB decode in progress. The 2.3 GB figure describes the implementation this
RFC deliberately did not write.

Absolute peak RSS is dominated by the loaded checkpoint (~730 MB baseline),
not by batching.

## 3.3 Embedding equivalence

Batching must change *how* the work is scheduled, never *what* is computed.

| batch size | mean cosine vs batch-of-1 | min cosine | max component delta |
| --- | --- | --- | --- |
| 2 | 1.000000 | 1.000000 | 0.0 |
| 4 | 1.000000 | 1.000000 | 1.19e-07 |
| 8 | 1.000000 | 1.000000 | 1.19e-07 |
| 16 | 1.000000 | 1.000000 | 1.19e-07 |
| 32 | 1.000000 | 1.000000 | 1.19e-07 |
| 64 | 1.000000 | 1.000000 | 1.19e-07 |

1.19e-07 is one float32 ULP at unit magnitude — the arithmetic is the same,
reassociated. Cosine similarity, which is what the HNSW index ranks on, is
unchanged to six decimal places. Batching cannot reorder a search result.

## 3.4 Gate outcomes

| gate | threshold | measured | outcome |
| --- | --- | --- | --- |
| Inference batching (§5) | ≥1.3× | 1.30× at batch 8; 1.38× at batch 32 | **passed — implemented** |
| Bulk metadata prefetch (§7.1) | none; mandatory | — | **implemented** |
| Bulk upsert writes (§7.2) | database >~5% of runtime | **9.2%** | **passed — implemented** |

## 3.5 The default batch size — where this RFC disagrees with its brief

The RFC-024 brief instructed a default of 8, reasoning from an inherited
measurement in which the curve "plateaus hard after 8", with batch 8
capturing 87% of all available speedup and batch 32 buying "almost nothing"
at 4× the decoded-image memory.

**Neither half of that premise reproduced here, and both are recorded rather
than quietly adopted:**

- Batch 8 captures **77%** of the speedup available out to 64
  ((1.30−1)/(1.39−1)), not 87%. Batch 32 is measurably faster than batch 8 —
  0.3473 vs 0.3670 s/image, about 5%, which is 0.5 h off a 100,000-image run.
  It is a real difference, not noise, though a modest one.
- Batch 32 does **not** quadruple decoded-image memory over batch 8. §3.2
  measured +54 MB, and that memory is preprocessed tensors, whose size does
  not depend on the source photo at all. The concern the brief raised is
  precisely the one §9's decode strategy removes.

**The shipped default is nonetheless 8.** With the curve this flat between 8
and 64, the remaining argument is not speed but headroom: 8 is the setting
that behaves predictably on the smallest machine anyone might run this on,
and `BATCH_SIZE` exists so a machine with memory to spare can be told to use
more. A reader of this RFC who wants the last 5% should set `BATCH_SIZE=32`
and can do so knowing what it costs (+54 MB), which is the information the
inherited table could not supply.

---

# 4. SHA-256 content hashing

## 4.1 The missing step

`ARCHITECTURE.md` §16 specifies a cost-ascending check. RFC-024 completes it:

1. Is there a row for this file?
2. Have `file_size` or `file_modified_at` changed?
3. **Only if step 2 says "possibly": hash the bytes and compare.**

Implemented as `ContentHasherPort` (Domain) with `Sha256ContentHasher`
(Infrastructure). Hashing is filesystem I/O, so it cannot live in Application;
the port mirrors `EmbeddingModelPort` exactly, and the Application layer
depends only on the contract. The hasher streams the file in 1 MiB chunks and
never calls `read()` unbounded — `Path.read_bytes()` would produce an
identical digest and allocate the whole file, immediately before the batch
inference that is already the memory-hungry stage.

The decision itself lives in `plan_indexing()`, a pure function of
(candidate, stored metadata, hasher). It returns one of three actions:

| action | when | what happens |
| --- | --- | --- |
| `SKIP_UNCHANGED` | size and mtime both match | nothing; the file is never opened |
| `REFRESH_METADATA` | metadata moved, hash matches | update metadata, **keep the embedding** |
| `EMBED` | new file, or hash differs | encode and write a full row |

**A new file is hashed even though step 1 has already condemned it to
embedding.** That is not a violation of "never hash before the timestamp and
size checks" — those checks have already run and already returned "process
this". It is what makes step 3 possible on the *next* run: a row persisted
without a hash reads back as `NULL`, and skipping it here would leave the
whole mechanism permanently dormant for every file the system ever indexes.

A stored hash of `NULL` never matches a computed one. Rows written before this
RFC carry `NULL` and were deliberately not backfilled (§14), so "unknown"
costs one re-embed rather than risking a wrongly skipped one.

## 4.2 The identity constraint

RFC-022 §7.1 makes `ImageId` **path**-derived on purpose, so that
byte-identical twins produce two rows. `content_hash` is a change-detection
field and nothing else. It is not unique, not indexed, and never used for
lookup, deduplication, or identity.

Three things enforce this rather than merely documenting it:

- `test_the_hash_does_not_participate_in_identity` writes two images with the
  same digest and asserts both rows exist.
- `test_identical_content_at_two_paths_produces_two_rows` (RFC-022, unchanged)
  still passes.
- `test_rfc_024_migration_leaves_content_hash_unindexed_and_not_unique` fails
  if a later migration adds a unique constraint or an index — the first step
  down the road towards the hash acquiring identity semantics.

## 4.3 What it saves — measured

Cold-index the demo corpus, then `utime` every file to a new mtime without
changing a byte, then re-index:

| run | elapsed | indexed | skipped (unchanged) | skipped (hash) |
| --- | --- | --- | --- | --- |
| cold index | 23.274 s | 45 | 0 | 0 |
| re-scan, untouched | 0.024 s | 0 | 45 | 0 |
| **after touching every mtime** | **0.292 s** | **0** | **0** | **45** |
| re-scan after the touch | 0.019 s | 0 | 45 | 0 |

Before this RFC, that third row was a full re-index: **~22.0 s of inference
for 45 images**, and 12.9 hours for 100,000. The hash step reduces it to
0.292 s — a **75× saving on that path** — and the fourth row shows the
refreshed metadata was written back, so the cost is paid once rather than on
every subsequent run.

This is a larger unit win than anything batching offers, and it comes from
reading files rather than from not reading them.

---

# 5. The batching contract

## 5.1 Options considered

| option | verdict |
| --- | --- |
| **1. `encode_images()` on `EmbeddingModelPort`, abstract** | Rejected. Every implementation, including `FakeEmbeddingModel` and every test double, would have to grow code for an optimization it does not have. |
| **2. Keep the port single-image, batch inside the adapter** | Rejected. The adapter cannot know when a batch is complete without an explicit flush, which hides latency and puts buffering state in a component that is otherwise stateless per call. |
| **3. Separate optional `BatchEmbeddingModelPort`** | Rejected. Forces an `isinstance`/`hasattr` probe into the Application layer — a runtime type test standing in for a contract, plus a second code path to keep correct. |
| **4. `encode_images()` on the port, with a concrete default** | **Chosen.** |

## 5.2 The decision

```python
def encode_images(self, images: Sequence[Image]) -> list[EmbeddingVector]:
    return [self.encode_image(image) for image in images]
```

Concrete, not abstract. An implementation with a faster path overrides it;
one without inherits a correct loop and writes nothing. This gives option 3's
"implementations may ignore batching" property with no probe and no branch in
Application, and option 1's honesty about what the contract offers.

`FakeEmbeddingModel` was not modified at all — it inherits the default and
remains the test double, as RFC-023 §18 requires.

The port's docstring names no tensors, devices, or checkpoints;
`test_the_batch_capability_did_not_leak_implementation_vocabulary` enforces
that against the specific words a batch API invites.

## 5.3 Where batching happens

`IndexOrUpdateImageUseCase.execute(image, ...)` did the skip-check, the embed,
and the persist for exactly one image in one call. That signature cannot
batch; calling it N times in a loop is still N sequential single-image calls.

**The chosen shape is decomposition (brief option 1).** The skip decision was
extracted into `plan_indexing()`, a pure function, and a new Application
coordinator assembles batches from its survivors.

### Component → responsibility after this RFC

| component | responsibility |
| --- | --- |
| `FilesystemImageProvider` | Discovery. Unchanged. Still sorts (§10). |
| `IndexingWorker` | Orchestration and logging. Streams discovered files as `IndexCandidate`s, calls the coordinator once, logs the summary and every failure. Also hosts the CLI composition root. |
| `plan_indexing()` *(new)* | The three-step skip decision, for one candidate. Pure: no writes, no batching, no logging. |
| `IndexOrUpdateImagesUseCase` *(new)* | The pipeline. Prefetch windows → skip decisions → inference batches → persistence batches. Owns error isolation and the run summary. |
| `IndexOrUpdateImageUseCase` *(kept)* | The single-file entry point. Built from `plan_indexing()`, so no logic is duplicated, but **errors propagate to the caller** instead of being collected. |
| `ClipEmbeddingModel` | Adds `encode_images()`. Still swallows nothing. |
| `PostgresImageRepository` | Adds `get_index_metadata_many()`, `save_indexed_many()`, `update_index_metadata()`. |

`IndexOrUpdateImageUseCase` was **kept, not retired**. It is the right
contract for indexing one known file: a future single-file reindex or
file-watcher path wants to be told that its one file failed, not handed a
report saying zero of one succeeded. Because both paths call `plan_indexing()`,
there is exactly one implementation of the incremental check.

`IndexingWorker` remains dependency-injected and constructs nothing —
`test_worker_does_not_construct_infrastructure_dependencies_internally`
still passes unchanged.

---

# 6. Error isolation under batching

**The most important correctness requirement in this RFC.**

```
batch of N
    |  fails as a unit
    v
retry the same N individually
    |
    v
N-1 succeed; 1 is reported with its real path and its real exception
```

The common case pays nothing: batches succeed and the retry never runs. The
failure case restores exactly the per-file isolation RFC-021 provided.

Four properties, each tested:

- **The original exception, never a wrapper.** `IndexingFailure` carries the
  exception object itself. An operator seeing `RuntimeError("indexing
  failed")` learns nothing actionable.
- **The offending path.** Carried alongside.
- **The degraded batch is recorded, not hidden.** When the retry then succeeds
  for every image, the run would otherwise report no failures at all and the
  only trace of trouble would be the missing time. `BatchFallback` captures
  the batch-level exception and the paths involved; the worker logs it at
  WARNING.
- **A batch of one skips the batch attempt entirely.** It has no per-call
  overhead to amortize, and attempting it would only run the same failing
  call twice before reporting the same error.

`ClipEmbeddingModel` was **not** given a broad `try/except`. RFC-023 §4's
decision that the adapter swallows nothing stands, and this RFC depends on it:
the adapter failing loudly is what lets the coordinator find out which file
was responsible.

**Acceptance test.** The RFC-022 hard-case corpus contains `zero_byte.jpg` and
`truncated.jpg` with `expect_with_pixel_decoding=FAILED`. The slow test
`TestHardCasesWithRealPixelDecoding` is now parametrized over batch size 1 and
8, so every assertion in it is a sequential-vs-batched comparison against the
real checkpoint. At batch 8 a single batch contains both broken files, so the
retry path is genuinely exercised rather than merely available. Both
parametrizations produce identical outcomes for all 14 cases.

Isolation also covers the two stages batching did not create:

- **Hashing** reads the file, so it fails for the same reasons indexing does
  (deleted between discovery and processing, permissions, unreadable media).
  Guarded per file.
- **Persistence** is guarded per row through the bulk-write fallback (§7.2).

Live demonstration, from the CLI against a 4-file corpus containing one
zero-byte JPEG:

```
Discovered:          4
Indexed:             3
Failed:              1
Inference:
  batches:           1
  fallbacks:         1   (batch failed, retried per image)
```

---

# 7. Persistence

## 7.1 Bulk metadata prefetch — mandatory, and a Big-O fix

This is not a throughput optimization and must not be conflated with one. On
a re-index where most files are unchanged, the skip path issued one
`get_index_metadata()` SELECT per discovered file. At 100,000 images with
80,000 unchanged, that is 80,000 round trips **before inference runs**,
regardless of how fast the model is.

`get_index_metadata_many(image_ids)` answers for a whole window in one query,
and the skip decision consults the result instead of the database.

It selects the four metadata columns by name rather than hydrating whole
`ImageModel` rows. `embedding` is a 512-float vector, so loading full rows
would drag roughly two kilobytes per image across the wire to answer a
question decided by two scalars — on the exact path that exists to make
re-scanning a large unchanged collection cheap.

Measured: a re-scan of the unchanged 45-image corpus completes in **0.024 s**
(0.53 ms/file, including discovery and entity construction).

Absent ids are omitted from the result rather than mapped to `None`, so
"no row" (new file) stays distinguishable from "row with empty metadata"
(written by `IndexImageUseCase.save()`), which the skip decision must treat
as changed.

## 7.2 Bulk upsert writes — gated, then implemented, and the gate's premise was wrong

The brief instructed: implement inference batching and the prefetch alone,
measure, and build bulk writes only if the database is still more than ~5% of
runtime. **It was 9.2%, so they were built.** What the follow-up measurement
showed is more interesting than the gate itself.

### The first implementation barely helped

Collapsing 45 commits into 6 moved the database share from 9.8% to 9.5%.
Profiling the run explained why:

| operation | measured |
| --- | --- |
| single commit of 45 staged rows | **2.5 ms** |
| everything else, per row | **~48 ms** |

**The commit was never the cost.** The cost was the read half: `session.get()`
per record issued one SELECT per row, and — worse — each of those SELECTs
triggered an autoflush of the rows staged so far, so SQLAlchemy emitted an
INSERT round trip per record anyway. "One commit per batch" had collapsed the
one part of the work that was already free.

Two hypotheses were tested and rejected on the way:

- **HNSW index maintenance.** 45 raw inserts of 512-dim vectors into a table
  *with* the cosine HNSW index took 54.5 ms; into an identical table
  *without* it, 53.7 ms. Index maintenance is not the cost.
- **torch thread contention after a forward pass.** Persistence measured
  48.2 ms/row with `FakeEmbeddingModel` and 47.4 ms/row with
  `ClipEmbeddingModel`. Nothing to do with torch.

### The real bulk write

`save_indexed_many()` now loads every existing row for the batch in one
`IN (...)` query and stages the whole batch inside `no_autoflush`, so a batch
reaches the database as **one read, one flush, one commit**.

A/B on the real pipeline, same corpus, fresh rolled-back transactions:

| write path | persistence | per row | database share of run |
| --- | --- | --- | --- |
| per-row (pre-RFC-024) | 2243.9 ms | 49.87 ms | **9.2%** |
| **bulk (RFC-024)** | **411.0 ms** | **9.13 ms** | **2.5%** |

**5.5× faster persistence**, and the database drops from a visible fraction
of the run into the noise.

### Transactional semantics vs error isolation

"One transaction per batch" and "isolate errors per file" genuinely conflict:
a strict transactional batch of 8 discards all 8 rows when row 5 violates a
constraint, throwing away embeddings that cost ~3 s of CPU.

Resolved the same way as §6: **attempt the bulk write; on failure, fall back
to per-row writes within the batch**, so only the genuinely bad row is lost.
The fallback is counted separately from the inference fallback, because "the
model choked on a file" and "the database rejected a row" are different
problems for whoever is reading the log.

**This uncovered a latent defect.** `save_indexed()` never rolled back a
failed commit, so SQLAlchemy left the session in a pending-rollback state and
the *next* row failed with `PendingRollbackError` instead of succeeding —
turning one bad row into a cascade through the rest of the run. The bug
predates this RFC (the RFC-021 per-file loop would have hit it too) but the
per-row fallback walks straight into it, so `save_indexed()` now rolls back
and re-raises. `test_a_rejected_row_does_not_poison_the_next_save` pins it.

## 7.3 Are the two batch sizes the same? Yes, deliberately

The persistence batch **is** the inference batch, and this is a decision
rather than an accident:

- The rows to write are exactly the output of the batch just encoded.
  Holding them back to fill a larger persistence batch would keep more
  512-float embeddings alive for no measured gain.
- It preserves the property that a batch's work is durable before the next
  batch starts, which is what makes crash-restart cheap under the incremental
  skip (§13).
- One knob is easier to reason about than two, and §7.2's measurement gives
  no reason to want the second.

Should a future measurement favour decoupling, `Settings` is where the second
knob goes.

---

# 8. Incremental skip precedes batch assembly

The model must never receive an image that already has a valid embedding.

```
1000 discovered
    v
 800 unchanged  -> skipped before any batch is formed
    v
 200 candidates -> batches of N
```

Batches are assembled **after** the skip decision, from survivors only, and
are never padded with already-indexed images. Enforced by:

- `test_a_second_run_over_an_unchanged_corpus_invokes_the_model_zero_times` —
  the adapter invocation count on a second run is zero, and
  `inference_batches` is 0.
- `test_batches_contain_only_the_survivors_of_the_skip_decision` — six
  indexed files, two changed, batch size 4: one batch of 2, not one of 4
  topped up with already-indexed rows.
- `test_an_unchanged_candidate_is_never_hashed` — the skip path does not even
  open the file.

---

# 9. Memory ceiling

The decode strategy, enforced in `ClipEmbeddingModel._preprocess()`:

```
open -> decode -> convert to RGB -> preprocess to the model input size
     -> discard the full-size decoded image -> accumulate only the small tensor
```

Preprocessing is deliberately a per-image loop rather than a single
`processor(images=[...])` call. The full-size decoded image is a local of
`_preprocess()` and nothing else, so it becomes unreachable the moment the
method returns. What accumulates across a batch is N fixed-size tensors,
whose size does not depend on the source photo at all.

The numbers that make this real:

- demo corpus at 1024×768 decodes to ~2.4 MB; a batch of 32 would be ~75 MB
- a 24-megapixel photo decodes to ~72 MB; a batch of 32 **would be ~2.3 GB**
  if decoded images were accumulated

**The demo corpus cannot expose this bug** — a test using only 1024×768
images passes either way while the production path exhausts memory on a
user's real photos. Two tests are aimed at it directly:

- `test_only_one_decoded_image_is_alive_at_a_time`, parametrized over batch
  sizes 2/4/8 on deliberately large synthetic images, uses `weakref.finalize`
  to count how many full-size decoded images are live simultaneously and
  asserts the peak is **1**.
- `test_the_batch_tensor_is_fixed_size_regardless_of_source_resolution`
  asserts the same invariant holds across two different source sizes.

The structural assertion was chosen over an RSS assertion inside the test
suite, as the brief permits, and it is the stronger of the two: RSS is noisy
and platform-dependent, while "no full-size decode outlives its own
preprocessing step" is exactly the invariant that keeps a batch of large
photos affordable. RSS *is* measured, per batch size, by the benchmark
(§3.2), where noise can be tolerated and the absolute numbers are the point.

---

# 10. Pipeline stages

```
DISCOVERY -> SKIP DECISION -> PREPROCESSING -> BATCH INFERENCE -> PERSISTENCE
```

Composed as a stream, with **no queues, no threads, no async, no
producer/consumer machinery**. Generator composition is sufficient and keeps
memory bounded: `IndexingWorker._candidates()` is a generator, the coordinator
consumes it lazily in windows, and the whole collection is never materialized.
`test_candidates_are_consumed_lazily` asserts the coordinator never runs more
than one window ahead of what it has persisted.

Two window sizes appear, and they are deliberately different (§13):
`metadata_prefetch_size` bounds a read of scalars; `batch_size` bounds decoded
pixels in memory.

**Load-bearing detail:** `FilesystemImageProvider.discover()` sorts its
results. RFC-024 relies on that and does not change it — determinism in
discovery order is what makes batching reproducible, and what would make any
future path checkpoint meaningful.

---

# 11. Metrics and observability

A run returns an `IndexingSummary` — counters and timings as data, so a test,
a benchmark, or a future scheduler can assert on them instead of parsing log
output. `format_report()` renders it:

```
Indexing finished

Discovered:         45
Skipped:             0   (unchanged)
Skipped (hash):      0   (mtime changed, content identical)
Indexed:            45
Failed:              0

Inference:
  images:           45
  batches:           6
  avg batch:     3.342s
  throughput:      2.2 img/s
  fallbacks:         0   (batch failed, retried per image)

Persistence:
  rows:             45
  batches:           6
  avg write:     0.071s
  fallbacks:         0   (bulk failed, degraded to per-row)
  total:          0.42s   (2.1% of elapsed)

Total:
  elapsed:       20.60s
  throughput:      2.2 img/s
```

Two lines earn their place specifically:

- **`Skipped (hash)`** counts files that would have been re-embedded before
  this RFC. It is the number that proves §4 is paying for itself.
- **`(2.1% of elapsed)`** is the §7.2 gate, reported by every run rather than
  measured once in a benchmark. If a future change makes the database
  expensive again, the next person will see it without being told to look.

The summary is returned by the Application layer and logged by Infrastructure.
That keeps the logging factory out of Application entirely; the existing
`app.infrastructure.logging` factory is used, no second logging mechanism was
introduced, and no metrics or telemetry dependency was added.

---

# 12. Worker CLI entry point

```
python -m app.infrastructure.workers.indexing_worker --root PATH
```

**Path discrepancy, resolved.** `AI_Context.md` promised
`infrastructure.workers.indexing_worker`, without the `app.` prefix. That form
has never been runnable: the package is `app.infrastructure`, and
`pyproject.toml` puts `backend/` on `pythonpath`, not `backend/app/`. The
documentation was corrected to match the code, since changing the code would
mean restructuring the package to satisfy a line of prose. `AI_Context.md` now
carries the working command.

**`--root` is required and has no default.** Not even
`settings.indexing_root_path`, following the reasoning already documented in
`dataset_tools/seed_demo.py`: defaulting there would silently start indexing a
user's real photo collection the first time they configure
`INDEXING_ROOT_PATH` and run the command from habit. Pointing the indexer at
a directory is a decision the operator makes explicitly, every time.

`main()` is a composition root — the one place allowed to know every concrete
class at once — and reuses `app.presentation.dependencies.get_embedding_model`
rather than building a second DI mechanism, which `AI_Context.md` forbids.
Session lifetime follows `seed_demo.py`'s established pattern (`SessionLocal()`
in a `try/finally`), because the container's `get_image_repository()` returns
a repository with no handle to close the session it opened.

The provider imports inside `main()` are function-local, and that is not a
trick to dodge a layer check. Most of the test suite imports `IndexingWorker`;
a module-level `from app.presentation.dependencies import ...` would drag
Presentation — and through it `ClipEmbeddingModel`, torch, and transformers —
into every one of those imports for a CLI that is not being run.

---

# 13. Configuration

| setting | before | after | why |
| --- | --- | --- | --- |
| `batch_size` | 16, **unused** | **8**, read by the worker CLI and `seed_demo` | Now actually wired. Default from §3.5. |
| `metadata_prefetch_size` | — | **512** *(new)* | §7.1 |
| `worker_count` | 1, unused | unchanged, still unused | Multi-process is out of scope (§17) |

The existing `batch_size` was reused rather than shadowed by a new knob —
adding a second overlapping setting for the same concept would leave a stale
16 sitting next to a measured 8.

`metadata_prefetch_size` is a genuinely separate concern, not an overlapping
knob, and `settings.py` says why in full: prefetch reads four scalar columns
and wants a large window; inference holds decoded pixels and wants a small
one. Coupling them would force a bad compromise in both directions — 8 ids per
SELECT is barely better than the per-file round trips §7.1 exists to remove,
and 512 decoded images at once would exhaust memory on real photos.

Both are declared in `.env.example` with the reasoning attached. No batch size
is hardcoded anywhere; the Application layer takes both as required
constructor arguments, so a composition root must supply them from `Settings`
and cannot silently fall back to a literal.

---

# 14. Database migration

`26058b9e1d9a_add_image_content_hash`, `down_revision = db526438ced5`
(RFC-023, verified as the head before writing).

```python
op.add_column(
    "images",
    sa.Column("content_hash", sa.String(length=SHA256_HEX_LENGTH), nullable=True),
)
```

64 characters because a hex-encoded SHA-256 digest is exactly that long — the
length is physical schema, not a guess. Nullable, unindexed, not unique (§4.2).
`downgrade()` drops only the new column.

**Existing rows keep `content_hash = NULL` and are not backfilled.** A
backfill would have to read every indexed file from disk to save a one-time
re-embed that the incremental check already absorbs. `NULL` reads as "unknown,
cannot confirm unchanged" and falls through to re-embedding.

Verified against the real database after applying:

```
                            Table "public.images"
      Column      |           Type           | Collation | Nullable | Default
------------------+--------------------------+-----------+----------+---------
 id               | uuid                     |           | not null |
 path             | character varying        |           | not null |
 filename         | character varying        |           | not null |
 extension        | character varying        |           | not null |
 file_size        | bigint                   |           |          |
 file_modified_at | timestamp with time zone |           |          |
 embedding        | vector(512)              |           |          |
 content_hash     | character varying(64)    |           |          |
Indexes:
    "pk_images" PRIMARY KEY, btree (id)
    "ix_images_embedding_hnsw" hnsw (embedding vector_cosine_ops)
    "uq_images_path" UNIQUE CONSTRAINT, btree (path)
Check constraints:
    "ck_images_file_size_non_negative" CHECK (file_size >= 0)
```

`embedding` is still `vector(512)` and `ix_images_embedding_hnsw` still uses
`vector_cosine_ops`. `test_earlier_migrations_were_not_rewritten` was extended
to assert the RFC-023 migration was not edited — it is the newest applied
revision and therefore the tempting place to "just add a column".

---

# 15. Testing strategy

**+132 tests** (297 → 429 in the fast suite; 26 → 48 slow). The default
`pytest` run remains offline and deterministic, verified with `HF_HUB_OFFLINE=1`
and an empty `HF_HOME`. Every test touching a real checkpoint is marked `slow`
and deselected by default.

| area | covered |
| --- | --- |
| **Hashing** | digest matches `hashlib`; reads are bounded (a `Path.open` wrapper records every requested size — the only observable difference between the chunked loop and `read_bytes()`, since both produce a correct digest); empty file hashes rather than failing; single-byte change alters the digest; missing file raises rather than returning a sentinel |
| **Skip decision** | unchanged metadata never reaches the hasher; touched-but-identical skips the embedding *and* writes the refreshed metadata back; genuinely changed re-embeds; `NULL` stored hash forces re-embedding; new files are hashed so the next run can use the check; byte-identical twins still produce two rows |
| **Batching** | batched and sequential produce equivalent embeddings (pipeline level with fakes, adapter level against the real checkpoint at batch 2/4/8); order is preserved; one forward pass per batch; an implementation with no batch support still works; a wrong return count stops the run |
| **Error isolation** | a batch with one corrupt file indexes the other N-1; the failure carries the real path and the real exception type and message; the degraded batch is recorded; a batch of one is not attempted twice; hashing and persistence failures are isolated per file; **the whole RFC-022 hard-case corpus produces identical outcomes at batch 1 and batch 8 against the real checkpoint** |
| **Persistence** | bulk upsert writes every row; a constraint violation discards the whole batch; the session survives a failed batch; the per-row fallback saves everything except the bad row; a rejected row does not poison the next save; bulk prefetch returns exactly what N individual `get_index_metadata()` calls return |
| **Memory** | peak live full-size decodes stays at 1 as batch size grows, on deliberately large synthetic images |
| **Metrics** | counters exact against a corpus with a known mix of new / unchanged / hash-identical / corrupt files |
| **CLI** | `--root` is required; no default points at `indexing_root_path`; `main()` composes the real pipeline and closes its session |
| **Boundaries** | Domain and Application import no AI library; the batch method leaked no implementation vocabulary; the hasher implementation is not imported above Infrastructure; use cases import no `sqlalchemy`/`psycopg`/`pgvector`/`alembic` |

Database tests use the SAVEPOINT-isolated `db_session` fixture and leave no
rows behind.

---

# 16. Performance against `ARCHITECTURE.md` §22

| target | measured | verdict |
| --- | --- | --- |
| Index throughput > 1 image/sec on CPU | **2.2 img/s** end to end at batch 8, hashing and persistence included | ✅ met, ~2× margin |
| Supported collection 100,000+ images | **~10.5 h** for a cold index at batch 8 | ✅ feasible |
| Startup time < 5 s | models still load lazily | ✅ unchanged |
| Search latency < 1 s | not this RFC (RFC-025) | — |

The ~10.5 h figure is built from steady-state per-image costs rather than
extrapolated from the 45-image wall clock, whose first batch carries a
one-time ~5 s checkpoint load: inference 0.3670 s + persistence 0.0091 s +
discovery/hashing/bookkeeping ~0.0029 s ≈ 0.379 s/image.

Against RFC-023's 12.90 h single-image baseline, that is a **~19% reduction**
for a cold index of a collection where nothing can be skipped — the case the
pipeline can improve least. The large win is elsewhere: a re-scan of an
unchanged collection now costs **0.53 ms/file** (~53 s for 100,000 images),
and a collection whose mtimes were disturbed without its bytes changing costs
6.5 ms/file instead of a full 12.9-hour re-embed.

---

# 17. Alternatives considered

| alternative | why not |
| --- | --- |
| **Abstract `encode_images()` on the port** | Forces every implementation, including test doubles, to write batching code for an optimization it does not have (§5.1). |
| **`BatchEmbeddingModelPort` capability probe** | Pushes `isinstance` into Application and doubles the code paths (§5.1). |
| **Batching inside the adapter behind an implicit flush** | Hides latency; needs buffering state in a per-call component (§5.1). |
| **Retiring `IndexOrUpdateImageUseCase`** | The single-file contract is genuinely different: it should raise, not report. Kept, sharing `plan_indexing()` (§5.3). |
| **Default `batch_size = 32`** | 5% faster and +54 MB. Rejected in favour of headroom on small machines, with the data recorded so it can be revisited (§3.5). |
| **Decoupled persistence batch size** | No measured benefit; costs a second knob and delays durability (§7.3). |
| **Backfilling `content_hash`** | Would read every indexed file to save a re-embed the incremental check already absorbs (§14). |
| **Threads / async / producer-consumer stages** | Explicitly out of scope. Generator composition already keeps memory bounded and stages measurable. |
| **Broad `try/except` inside `ClipEmbeddingModel`** | Would hide which file failed and why; RFC-023 §4 stands (§6). |

## Rejected by measurement

The most useful thing in this section is what the numbers killed:

- **"One commit per batch" as the bulk-write design.** Built, measured, and
  found to move the database share from 9.8% to 9.5%, because the commit cost
  2.5 ms and the per-row reads cost ~48 ms. Replaced with a design that
  collapses the reads (§7.2).
- **HNSW index maintenance as the suspected per-row cost.** 54.5 ms with the
  index vs 53.7 ms without it, for 45 inserts. Not the cost.
- **torch thread contention as the suspected per-row cost.** 48.2 ms/row with
  `FakeEmbeddingModel` vs 47.4 ms/row with `ClipEmbeddingModel`. Not the cost.
- **The inherited "plateau at 8" curve.** Did not reproduce; batch 8 captures
  77% of available speedup here, not 87% (§3.5).
- **The "batch of 32 costs 2.3 GB on 24 MP photos" concern.** Real for an
  implementation that accumulates decoded images; measured at ~1.2–1.5 MB per
  image in flight for the one that does not (§3.2).

---

# 18. Risks

| risk | mitigation |
| --- | --- |
| A batch failure silently degrades throughput on a corpus full of broken files | `fallbacks` is counted and logged at WARNING per occurrence |
| `batch_size` raised recklessly on a machine with large photos | Memory growth is linear and documented; `.env.example` says what the knob costs |
| An adapter returning the wrong number of embeddings | Raises immediately rather than degrading, so the contract violation cannot hide |
| The per-row fallback masking a systematic database problem | Persistence fallbacks are counted separately from inference fallbacks |
| Content hashing on very large files | Streams in 1 MiB chunks; hashing is only reached for files already destined for processing |
| A future RFC making `content_hash` unique or indexed | Migration test fails on either |
| Benchmark numbers taken on one machine | The benchmark is committed and reproducible; every number here names its conditions |

---

# 19. Non-goals

Confirmed absent from the implementation:

- vector similarity search, ranking, top-K (RFC-025)
- `Collections` table, `Collection` entity, `collection_id` foreign keys
- thumbnail generation, `width`/`height` extraction
- fine-tuning, LoRA, adapters
- quantization, ONNX, alternative inference backends
- multi-process or distributed workers — `worker_count` remains unused
- GPU backends beyond RFC-023's CUDA auto-detection
- changes to `FakeEmbeddingModel`
- the full 100,000-image benchmark run (RFC-026)

**On parallelism.** This RFC pulls one of two levers on the same bottleneck.
Batching amortizes fixed per-call overhead within one process; multiple worker
processes would let CPU-bound inference actually run concurrently, which
batching alone cannot provide, and is plausibly the larger win. It is also
unmeasured, and naively spawning processes around a PyTorch CPU workload can
saturate RAM and contend for the same cores. It needs its own benchmark and
its own RFC. Excluding it here means "not measured yet", not "does not matter".

---

# 20. Resumability — deliberately deferred

`ARCHITECTURE.md` §15 defines an `IndexingJobs` table with
`last_processed_path` as a checkpoint. **Not implemented, and the measurement
supports the deferral rather than merely excusing it.**

RFC-020's incremental skip already provides crash recovery: re-running after
an interruption skips everything already indexed. With §7.1's bulk prefetch,
that re-scan costs **0.53 ms/file — about 53 seconds for 100,000 images**,
against a cold index of ~10.5 hours. `last_processed_path` would save a
fraction of that 53 seconds. It is a restart *optimization*, not a
correctness requirement.

Independently, `IndexingJobs` as specified carries a `collection_id` foreign
key to a `Collections` table that does not exist and is out of scope (§19), so
implementing it would mean designing a table around a relationship this RFC
is not allowed to create.

---

# 21. Future work

- **Multi-process indexing.** The unpulled lever (§19). Needs its own
  benchmark first.
- **Decoupled persistence batch size**, if a deployment ever shows the
  database mattering again. The report line in §11 is how it would be noticed.
- **Reduce the remaining ~9 ms/row.** The bulk write is 5.5× better but not
  free; a PostgreSQL-native `INSERT ... ON CONFLICT DO UPDATE` would skip the
  ORM read entirely.
- **`last_processed_path`**, once `Collections` exists and if the 53-second
  re-scan ever becomes a real complaint.
- **Backfilling `content_hash`** for pre-RFC-024 rows, if a collection is
  large enough that the one-time re-embed is worse than one full read pass.
- **Adaptive batch size** from available memory, rather than a fixed default
  chosen for the smallest plausible machine.

---

# 22. Deliverables

**New**

| file | purpose |
| --- | --- |
| `backend/app/domain/services/content_hasher_port.py` | `ContentHasherPort` |
| `backend/app/application/use_cases/indexing_plan.py` | `IndexCandidate`, `IndexAction`, `IndexPlan`, `plan_indexing()` |
| `backend/app/application/use_cases/index_or_update_images.py` | The batch coordinator, `IndexingSummary`, `IndexingFailure`, `BatchFallback` |
| `backend/app/infrastructure/filesystem/sha256_content_hasher.py` | Streaming SHA-256 hasher |
| `backend/alembic/versions/26058b9e1d9a_add_image_content_hash.py` | The migration |
| `scripts/benchmark_indexing.py` | The benchmark behind every number in §3 |
| `docs/rfcs/rfc-024-embedding-pipeline.md` | This document |
| `backend/tests/domain/test_content_hasher_port.py` | Port contract |
| `backend/tests/infrastructure/filesystem/test_sha256_content_hasher.py` | Hasher, including streaming |
| `backend/tests/application/test_index_or_update_images.py` | Coordinator: batching, isolation, prefetch, metrics |

**Modified**

| file | change |
| --- | --- |
| `backend/app/domain/services/embedding_model_port.py` | `encode_images()` with a concrete default |
| `backend/app/domain/repositories/image_repository.py` | `save_indexed_many()`, `get_index_metadata_many()`, `update_index_metadata()` |
| `backend/app/domain/value_objects/index_metadata.py` | `content_hash` |
| `backend/app/domain/value_objects/indexing_record.py` | `content_hash` |
| `backend/app/application/use_cases/index_or_update_image.py` | Rebuilt on `plan_indexing()`; takes a `ContentHasherPort` |
| `backend/app/infrastructure/ai/clip_embedding_model.py` | `encode_images()`, `_preprocess()`, `_to_embedding_vectors()` |
| `backend/app/infrastructure/persistence/postgres_image_repository.py` | Bulk upsert, bulk prefetch, metadata update, rollback on failed write |
| `backend/app/infrastructure/persistence/in_memory_image_repository.py` | New port methods, atomic bulk write |
| `backend/app/infrastructure/database/models/image_model.py` | `content_hash` column |
| `backend/app/infrastructure/workers/indexing_worker.py` | Streaming candidates, summary logging, CLI entry point |
| `backend/app/infrastructure/config/settings.py` | `batch_size` 16 → 8; `metadata_prefetch_size` |
| `backend/dataset_tools/seed_demo.py` | Rewired to the batch coordinator |
| `.env.example` | `BATCH_SIZE=8`, `METADATA_PREFETCH_SIZE=512` |
| `AI_Context.md` | Corrected worker entry-point path |
| 14 test modules + `tests/application/fakes.py` | Rewiring plus the coverage in §15 |

---

# 23. Validation

| check | result |
| --- | --- |
| `pytest` | **429 passed**, 48 deselected, 1 xfailed |
| `pytest -m slow` | **48 passed** |
| `pytest` with `HF_HUB_OFFLINE=1` and an empty `HF_HOME` | **429 passed** — no downloads |
| `black --check .` | clean (121 files) |
| `ruff check .` | clean |
| `mypy` | **5 errors in 4 files** — the established pre-existing baseline, unchanged |
| `alembic heads` | `26058b9e1d9a (head)` — exactly one |
| `alembic history` | linear, 5 revisions, none rewritten |
| Real schema | `content_hash varchar(64)` nullable; `embedding vector(512)`; HNSW `vector_cosine_ops` intact |
| Development database | **0 rows** — nothing left behind |
| Domain / Application AI imports | none |
| Non-goals | no vector search, no `Collections`, no thumbnails, no fine-tuning, no quantization |

The CLI was additionally run for real against PostgreSQL on a four-file corpus
containing a zero-byte JPEG; it indexed 3, reported 1 failure with its path,
recorded 1 inference fallback, wrote `content_hash` on every row, and the rows
were removed afterwards.
