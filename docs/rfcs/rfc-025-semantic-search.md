# RFC-025 — Semantic Search

**Status:** Implemented
**Depends on:** RFC-022 (demo dataset), RFC-023 (CLIP adapter), RFC-024 (embedding pipeline)
**Migration:** none — see §5
**Measurement:** `experiments/rfc-025-semantic-search/measure_retrieval.py`

---

# 1. Context

RFC-023 gave SolidVision a real embedding model. RFC-024 turned indexing into
a pipeline that fills the `embedding` column incrementally, in batches, at 2.2
images/second. After both, a full demo corpus of 45 images sits in PostgreSQL
with 512-dimensional L2-normalized CLIP vectors and an HNSW index built for
cosine distance.

Nothing read any of it.

```python
def execute(self, query: str) -> list[Image]:
    """Return images from the repository for the supplied query."""
    self._embedding_model.encode_text(query)
    return self._repository.list()
```

That was the entire search implementation: encode the query, **discard the
vector**, return every row in insertion order. Both previous RFCs named this
as the next RFC's problem rather than quietly widening their own scope —
RFC-023 §17 lists "vector similarity search" as a non-goal and RFC-023 §12.5
explains why the retrieval-quality test stayed dormant; RFC-024 §19 repeats it.
`queries.json` had shipped with 25 ground-truth queries in RFC-022 and had
never once been scored against a real ranking.

# 2. Problem

Make this work, end to end, and prove it with the ground truth that already
exists:

```
"uma propriedade rural com um lago"
        ↓  langdetect + Marian
"a rural property with a lake"
        ↓  "a photo of {query}" + CLIP text tower
   512-d L2-normalized vector
        ↓  ImageRepository.search_similar()
   PostgreSQL / pgvector / cosine
        ↓
   ranked SearchHit[], with scores
```

The constraint that shapes every decision below: **the ranking belongs to the
database.** A correct implementation that pulled 100,000 vectors into Python
to sort them would satisfy every test in this RFC and be useless at the scale
`ARCHITECTURE.md` §22 targets.

# 3. Decision

| decision | outcome |
| --- | --- |
| Ranking location | Inside PostgreSQL, ordered by pgvector's `<=>` |
| Score | Cosine similarity in **[-1, 1]**, converted from distance in Infrastructure |
| New Domain type | `SearchHit(image, similarity)` |
| New port method | `ImageRepository.search_similar(embedding, limit) -> list[SearchHit]` |
| Scope of a search | Global over every known image (§8) |
| Page size | Injected default from `settings.top_k_results`, hard ceiling `MAX_SEARCH_LIMIT = 100` |
| Validation | Blank query and out-of-range limit raise domain errors |
| Migration | None. RFC-023's schema already fits (§5) |
| `settings.minimum_similarity` | **Removed** (§13) |

# 4. Architecture

Each layer holds exactly the part of search it can own without knowing the
others exist:

| layer | knows | does not know |
| --- | --- | --- |
| **Domain** | that a search returns ranked `SearchHit`s, that scores are cosine, that unindexed images never appear | SQL, pgvector, HNSW, CLIP, translation |
| **Application** | what makes a request usable — non-blank query, `limit` in range | how text becomes a vector, how a vector becomes a ranking |
| **Infrastructure (AI)** | langdetect, Marian, the prompt template, the text tower | that a search exists at all |
| **Infrastructure (persistence)** | `<=>`, `vector_cosine_ops`, distance → similarity, tie-breaks | what a query said, or in what language |

The use case is nine lines of real work. That is the point: the pipeline in §2
crosses four specialist components, and the only place it is *assembled* is a
layer that understands none of them.

## 4.1 Why `SearchHit` and not a score on `Image`

`Image` is frozen, compares by id, and means the same thing to every caller.
A similarity means nothing without the query that produced it — the same
image scores differently for every search, and would carry no score at all
when merely listed. Putting the number on the entity would let two `Image`
values for the same file hold contradictory data while still comparing equal.

`SearchHit` lives in Domain, beside `IndexingRecord` and `IndexMetadata`, for
the reason `IndexingRecord`'s docstring already gives: it is part of the
`ImageRepository` port's own contract, and a port whose return type lives in
Application is not self-contained.

## 4.2 The port contract

`search_similar()`'s docstring specifies behavior without naming a technology:
ordered best-first; images without embeddings never appear; fewer than `limit`
when candidates run out and `[]` when there are none; deterministic tie order;
global scope; and a dimension mismatch is an error rather than an empty
result. Every clause is a test in `test_search_similar_contract.py`, run
against all three implementations (§10.2).

# 5. Why there is no migration

RFC-023's `db526438ced5` already created everything search needs:

```python
embedding = Vector(512)                       # nullable
ix_images_embedding_hnsw USING hnsw (embedding vector_cosine_ops)
```

The column holds the vectors, the index is built for the operator this RFC
orders by, and the nullability is what lets an unindexed row exist and be
excluded. **Search is a read.** It stores nothing, needs no new column, no
new table, and no new index — so it gets no migration, and `alembic heads`
still reports `26058b9e1d9a` as the single head.

The literal `512` in `image_model.py` became a named constant,
`EMBEDDING_DIMENSION`, because search now has to validate query vectors
against the same number and two copies of a physical-schema fact are one copy
too many. The column definition and the migration are unchanged.

# 6. The query

```python
distance = ImageModel.embedding.cosine_distance(list(embedding.values))
select(ImageModel.id, ImageModel.path, ImageModel.filename, ImageModel.extension,
       distance.label("distance"))
    .where(ImageModel.embedding.is_not(None))
    .order_by(distance, ImageModel.id)
    .limit(limit)
```

Four details, each load-bearing:

**`<=>`, not anything else.** `cosine_distance` emits the operator the index
was created for. Ordering by L2 or inner product would silently abandon the
index *and* change the ranking.

**`WHERE embedding IS NOT NULL`.** A row written by `IndexImageUseCase.save()`
has no vector. Treating a missing vector as zeros, or appending unranked rows
once ranked ones run out, would put files nobody indexed ahead of files
somebody did.

**The id tie-break.** Without it, equally distant rows come back in whatever
order the plan produced — which changes between a sequential scan and an
index scan, i.e. as the table grows — and `limit` would then cut an arbitrary
one of them. §7.2 shows the tie-break costs nothing.

**Four columns, not the entity.** `select(ImageModel)` would drag a 512-float
vector back per hit to build a domain entity that has no embedding field. The
same argument RFC-024 §7.1 made for `get_index_metadata_many()`.

# 7. Approximate or exact? Measured, not assumed

HNSW is an **approximate** nearest-neighbour index. When the planner uses it,
the top-K it returns is not guaranteed to be the true top-K, and this RFC does
not promise exact ranking as a general property.

At today's scale the question is moot for a different reason: the planner does
not use the index at all — and §7.3 shows what it costs when it does. Measured with `EXPLAIN ANALYZE` on synthetic
512-dimensional unit vectors, the shipped query (tie-break included), rolled
back afterwards:

| rows | plan | execution |
| --- | --- | --- |
| 45 (the demo corpus) | Seq Scan → top-N heapsort | **0.36 ms** |
| 2,000 | Seq Scan → top-N heapsort | **9.09 ms** |
| 10,000 | **Index Scan** `ix_images_embedding_hnsw` → Incremental Sort | **1.90 ms** |

Full plans: `experiments/rfc-025-semantic-search/planner_explain_output.log`.

## 7.1 What this means for the demo corpus

Every retrieval number in §11 was produced by a sequential scan, so those
rankings are **exact** — 45 full cosine computations per query, no
approximation anywhere. The measurement is a measurement of the *model*, not
of the index's recall.

The corollary is stated rather than hidden: **RFC-025 has not validated HNSW
recall.** A 45-row "index sanity test" would be theatre — the planner would
ignore the index and the test would pass regardless of whether the index was
correct, empty, or built for the wrong operator. Recall at scale needs a
corpus large enough for the planner to choose the index, and belongs with the
100k benchmark RFC-024 §19 already deferred (RFC-026).

## 7.2 The tie-break does not cost the index

This was the one real risk in §6, and it was measured rather than reasoned
about: pgvector's index-ordered scan satisfies `ORDER BY <=>`, and a second
sort key could have forced the planner to abandon it and sort everything.

It does not. At 10,000 rows the planner chose `Index Scan` feeding an
**Incremental Sort** — the index supplies the distance order, and the sort
only breaks ties within groups of equal distance. Determinism and the index
are not in tension.

## 7.3 What changes when the index *is* used — measured by accident

The planner experiment above left the development database with an inflated
`pg_class.reltuples` (ANALYZE updates it in place, so a rolled-back
transaction does not undo it) and an HNSW index holding thousands of
dead-but-not-yet-vacuumed entries. The planner therefore chose the index scan
on a table with **3 live rows** — and the contract tests, green minutes
earlier, failed:

```
test_limit_is_respected_exactly[postgres]
    assert len(repository.search_similar(one_hot(0), limit=2)) == 2
E   AssertionError: assert 1 == 2
```

The query returned **1 of 3 live rows**. Not a bug in the query, and not a
transient: it is how an HNSW scan behaves. The scan explores at most
`hnsw.ef_search` candidates (40 by default), dead tuples are spent from that
budget before MVCC filters them out, and whatever survives is the answer.
`VACUUM ANALYZE images` restored both the statistics and the tests.

Two conclusions worth more than the accident that produced them:

**The port contract's "fewer than `limit` only when there are fewer
candidates" is exact at seq-scan scale and best-effort under ANN.** That is
not a promise the database can keep, so the guarantee is documented where it
is qualified — `PostgresImageRepository.search_similar()`'s docstring — rather
than quietly assumed everywhere.

**Index maintenance becomes operational surface at scale.** A collection with
heavy churn accumulates dead index entries that cost recall until autovacuum
catches up. Nothing in this RFC needs to act on that at 45 rows; whoever runs
the 100k benchmark does, alongside `ef_search`.

---

# 8. Global scope, deliberately

Search covers the whole `images` table. There is no collection filter, and
that is a decision rather than an omission: `Collection` is a one-line
placeholder entity, `ImageModel` has no `collection_id`, and no migration
creates one. Scoping search by collection is a change to what a collection
*means*, and it needs the table, the foreign key, and the ownership rules
first — the same reason RFC-024 §20 refused to build `IndexingJobs` around a
relationship it was not allowed to create.

A path filter was also considered and rejected. It would have made the
database tests trivially isolatable (§10.1) by scoping every search to the
test's own directory — an Application concern invented to serve a test, in a
contract that will outlive it.

# 9. What the score means

`SearchHit.similarity` is **cosine similarity in [-1, 1]**.

pgvector's `<=>` returns cosine *distance* in [0, 2]; the repository returns
`1 - distance`. The conversion happens in Infrastructure, so nothing above it
knows a distance ever existed. The value is **not clamped and not rescaled**.

## 9.1 The measured distribution

Over all 1,125 (query, image) pairs — 25 queries × 45 images, real CLIP:

| statistic | value |
| --- | --- |
| minimum | **-0.1183** |
| maximum | **+0.3737** |
| mean | +0.1524 |
| best hit per query, min | +0.2352 |
| best hit per query, max | +0.3737 |
| best hit per query, mean | **+0.3003** |

Two things this makes concrete. **Negative similarities are real** — they
occur in an ordinary corpus with an ordinary query, so a [0, 1] contract
would have been wrong on the first day, not in some future edge case. And
**CLIP similarities are compressed**: a *correct* top hit scores ~0.30, not
~0.9. Anyone building a UI or a threshold on top of this must read the
distribution rather than intuition — which is exactly why §13 removes the
`minimum_similarity` setting instead of guessing a default for it.

This follows RFC-023 §6.1, which recorded measured L2 norms (~11.6 image,
~8.5 text) rather than assuming the model normalized its outputs.

# 10. Testing

## 10.1 Database isolation, and its caveat

Search tests are top-K queries over the entire table. Unlike
`test_demo_corpus_indexing.py`, they cannot defend themselves by asserting on
ids they created: a row committed to the shared development database by other
work can place itself *between* the expected results, and a recall number
computed over a contaminated table is wrong rather than noisy.

`empty_db_session` runs `DELETE FROM images` inside the outer transaction
`db_session` already opens, commits it at the session level (so a
`session.rollback()` in the code under test cannot resurrect the rows), and is
undone by the existing teardown rollback. No new infrastructure, nothing left
behind.

**The caveat, stated rather than buried.** This holds a write lock on every
existing row for the duration of the test and does not protect against a
concurrent writer in another process. That is acceptable because the suite is
single-process: `requirements.txt` pins `pytest` and `pytest-mock`, with no
`pytest-xdist`. It stops being acceptable the day parallel test execution
arrives.

A dedicated test database was considered and rejected as a separate RFC's
work, not because it is wrong: `EngineInstance` is a module-level singleton
built from `settings.database_url` at import time, there is one `.env` and one
`POSTGRES_DB` in `docker-compose.yml`, and a fresh database would need
`CREATE EXTENSION vector` plus all five migrations before the first test.
That is a change to how the whole suite reaches PostgreSQL, and it is future
work (§15).

## 10.2 One contract, three implementations

`tests/infrastructure/persistence/test_search_similar_contract.py` is
parametrized over `PostgresImageRepository`, `InMemoryImageRepository`, and
`FakeImageRepository` — 9 tests × 3 = **27**. The doubles are only useful
while they are indistinguishable from the real thing, and every clause of the
port contract is checked against all three:

| behavior | why it is here |
| --- | --- |
| identical / orthogonal / opposite rank in that order | the ordering itself |
| similarity ≈ 1.0 / ≈ 0.0 / **≈ -1.0** | the opposite case is what proves [-1, 1]; a clamp or a rescale passes the ordering test and fails this one |
| `limit` respected exactly | |
| fewer candidates than `limit` | a short page is not an error |
| NULL-embedding rows never appear, even as the majority | |
| empty table → `[]` | |
| ties resolve identically everywhere | ids seeded in reverse, so insertion order cannot be what produces the answer |
| wrong-sized vector raises, **including on an empty repository** | §10.3 |

The vectors are 512-dimensional one-hot axes, not 3-element toys. A
3-dimensional vector passes `EmbeddingVector` validation (which only rejects
empty), works fine in Python, and is rejected by the `vector(512)` column — a
test that would pass twice and fail once, for reasons unrelated to what it
checks. One-hot vectors also make the expected cosines exact rather than
approximate: 1, 0, and -1 by arithmetic.

## 10.3 Dimension mismatch: the silent wrong answer

Python's `zip` truncates to the shorter operand. A hand-rolled cosine over a
3-dimensional query and 512-dimensional rows returns a *plausible* number
computed from three dimensions — a wrong answer indistinguishable from a right
one. Both in-memory implementations therefore raise
`EmbeddingDimensionMismatchError` explicitly, mirroring the care
`save_indexed_many()`'s docstring already takes over atomicity.

PostgreSQL rejects a wrong-width vector too, but *unreliably*: the
`vector(512)` column only complains once a row is actually compared, so the
same bad call raises against a populated table and returns `[]` against an
empty one. `PostgresImageRepository` validates up front against
`EMBEDDING_DIMENSION`, which makes the failure identical in all three
implementations — and the contract test asserts it on an empty repository
precisely to pin that.

The two in-memory implementations share one `cosine_search()` function rather
than owning a cosine loop each. Two hand-written loops would be two chances to
drift from pgvector and from each other.

## 10.4 The dormant test, discharged

`tests/dataset/test_queries.py` carried an `xfail` cosine loop and a docstring
promising that *"when vector search lands, the test to write is one that
exercises `SearchImagesUseCase` end to end — not this hand-rolled cosine
loop."*

That is now `tests/dataset/test_semantic_search_e2e.py`. The loop is gone; the
structural checks on `queries.json` (paths exist, no overlap, no duplicates,
no orphaned aerial image) stay in the fast offline suite, because they depend
on two JSON files and no model.

## 10.5 End to end, under `slow`

Real CLIP, real PostgreSQL, real pgvector, all 25 queries. Marked `slow` and
deselected by the default `addopts`, following RFC-023 §12.1, so a plain
`pytest` stays offline.

The corpus is indexed **once per module** (~47 s of inference), not once per
test. Module scope rather than session scope is deliberate: the fixture holds
an open transaction over an emptied `images` table, and session scope would
stretch that lock across every other module's database tests.

The tests assert **aggregates, never individual results**. The checkpoint
takes 64.0% strict top-1 on this corpus, so nine of the 25 queries are
*expected* to rank something else first — pinning `result[0] == "file.jpg"`
per query would encode the model's noise as a requirement.

# 11. Results

Full run: `experiments/rfc-025-semantic-search/retrieval_run_output.log`.
45 images, 25 queries, `laion/CLIP-ViT-B-32-laion2B-s34B-b79K`, CPU,
2026-08-22.

## 11.1 Aggregates

| metric | measured |
| --- | --- |
| **Recall@5** (≥1 relevant image in the top 5) | **84.0%** (21/25) |
| Recall@1 | 64.0% |
| Recall@10 | 96.0% |
| relevant coverage@5 (mean fraction of a query's relevant set retrieved) | 70.0% |
| **hard-negative contamination@5** (top-5 slots holding a declared near-miss) | **12.8%** (16/125) |
| strict top-1 (bake-off metric) | 64.0% |
| pairwise (bake-off metric) | 80.4% |

Two metrics rather than one, because they fail differently. Recall asks
whether the right answer is on the page. Contamination asks what else is
sharing it — an index degenerating into "anything vaguely aerial" can hold
recall steady while filling every remaining slot with plausible wrong answers.

## 11.2 Where it fails, honestly

The four queries that miss at rank 5 are all the same failure, and it is the
one the dataset was built to provoke: **CLIP cannot separate a natural lake
from artificial fish ponds** on this corpus.

| # | query | top-1 returned |
| --- | --- | --- |
| 2 | "small natural lake on a farm, not an artificial pond" | `fish_ponds_02.jpg` |
| 4 | "swimming pool at a rural home, not a natural or artificial water body" | `fish_ponds_02.jpg` |
| 14 | "workshops and small businesses lining a commercial street" | `urban_suburban_overview` |
| 20 | "wide drone overview of a hillside town with a large..." | `urban_commercial_street` |

Queries 2 and 4 also show a known limitation of the model rather than of the
search: **CLIP has no reliable negation.** Both spell out what they do *not*
want ("not an artificial pond", "not a natural or artificial water body") and
both retrieve exactly the thing they excluded. A bag-of-concepts text tower
reads "artificial pond" as a topic, not as a prohibition.

This is the honest baseline of a 512-dimensional general-purpose checkpoint
chosen for speed with fine-tuning explicitly deferred (RFC-023 §3.6, §18) —
not a target, and not a defect in the search implementation.

## 11.3 Cross-check against the RFC-023 bake-off

The bake-off ranked **these 45 images** against **these 25 queries** with a
numpy cosine loop in a separate virtualenv. RFC-025 recomputes the same
quantities through the production path — L2-normalized vectors in a
`vector(512)` column, ordered by `<=>`.

| metric | bake-off (numpy) | RFC-025 (pgvector) | delta |
| --- | --- | --- | --- |
| strict top-1 | 60.0% | 64.0% | +4.0 pts = **1 query** |
| pairwise | 80.4% | **80.4%** | **0.0** |

**Pairwise matched exactly.** That is the stronger of the two results: it
reads the entire 45-image ranking rather than only its first slot — 3,150
(relevant, hard-negative) comparisons — and it is what a wrong operator, a
lost normalization, or a truncated vector would have destroyed. Strict top-1
differs by one query out of 25, where one query is worth 4 points; on a
25-query set that is the resolution of the instrument, and the two paths ran
in different virtualenvs against different transformers builds.

No divergence to explain, and therefore no implementation bug to hunt.

## 11.4 The regression floors, chosen after measuring

Measured first, written down second. The floors sit two queries away from the
measurement — 8 points, on a set where one query is 4:

| gate | measured | locked at | slack |
| --- | --- | --- | --- |
| Recall@5 | 84.0% | **≥ 76%** | 2 queries |
| contamination@5 | 12.8% | **≤ 20%** | 9 slots of 125 |
| strict top-1 | 64.0% | **≥ 56%** | 2 queries, and 1 below the bake-off |
| pairwise | 80.4% | **≥ 72%** | implementation check, not a quality gate |

One query flipping is noise — a torch or transformers upgrade moving float
arithmetic, a re-materialized corpus. Two flipping the same way is a signal.
A genuine break in the pipeline (no translation, wrong template, wrong
operator, results returned unranked) does not cost two queries; it costs ten.

## 11.5 Portuguese

One smoke query, and it is a full sentence on purpose: RFC-023 §7.1 measured
`langdetect` calling `fazenda` Turkish and `lago` Tagalog, so a one-word
Portuguese query reaches CLIP untranslated and would measure the raw-PT path
while claiming to measure translation.

`"uma propriedade rural com um lago"` translates and retrieves
`lake_property_01.jpg` inside the top 5, overlapping the English sentence's
top 5 in at least 3 of 5 slots. No Portuguese ground-truth set was created:
the bake-off already quantified the language gap (56.0% strict-PT(MT) against
60.0% strict-EN), and what was unproven until now is only that the translator
is wired into the *search* path at all.

One observation worth recording for a future reader: Marian emits this
particular sentence with a leading `". "`, so the prompt actually handed to
CLIP is `"a photo of . a rural property with a lake"`. Retrieval still works
and the translator is RFC-023's component, so nothing was changed here — but
it is a real artifact, visible in the log, and a plausible thing to clean up
when the translation path is next touched.

# 12. Performance against `ARCHITECTURE.md` §22

| target | measured | verdict |
| --- | --- | --- |
| Search latency < 1 s | **~90 ms** warm English query end to end | ✅ met |
| | ~400 ms warm Portuguese query (translation included) | ✅ met |
| | database share: 0.36 ms at 45 rows, 1.90 ms at 10,000 | ✅ negligible |
| Startup time < 5 s | models still load lazily | ✅ unchanged |

Breakdown of the English number: `encode_text()` over the 25 queries measured
**mean 89.7 ms, median 87.5 ms, max 128.7 ms** on CPU; the pgvector query is
under 2 ms at both sizes tested. **Search latency is the text tower**, not the
database — a fact worth knowing before anyone optimizes the SQL.

**The caveat is the cold start.** The first query of a process pays ~4.95 s to
load the CLIP checkpoint, and the first *Portuguese* query pays a further
~4.20 s for the Marian translator. Both blow the 1-second target once per
process. That is RFC-023's lazy-loading design working as specified (§10
there), and the fix — warming the model at startup — belongs to whichever RFC
introduces the HTTP layer, where "startup" first means something.

# 13. Configuration

## 13.1 `top_k_results` finally has a consumer

It has been configurable and unread since the settings module was written. It
is now the default page size, injected into `SearchImagesUseCase` by the
composition root — never imported by the use case, which would be an
Application dependency on Infrastructure that
`test_application_architecture.py` forbids. This follows `batch_size` and
`metadata_prefetch_size` exactly (RFC-024 §13).

`MAX_SEARCH_LIMIT = 100` is Application policy and lives with the use case: a
setting configures a default, it does not authorize an unbounded page.

Out-of-range limits **raise** rather than clamp. A caller that asks for 10,000
and silently receives 100 cannot tell that from a corpus containing 100
images.

## 13.2 `minimum_similarity` removed

```python
minimum_similarity: float = Field(default=0.0, ge=0.0, le=1.0, ...)   # deleted
```

Zero readers, and a **declared range that was wrong**: §9.1 measures scores
from -0.1183 upward, so `ge=0.0` would have rejected legitimate configuration
for a filter that did not exist. Leaving it in place would have left a config
key that looks supported, is not, and encodes a false claim about the score.

A similarity floor may well be worth having. It needs the distribution in §9.1
to choose a default, and a decision about what "no results" should mean to a
user — which is a product question, not a leftover field. `TOP_K_RESULTS`
keeps its `.env.example` entry; `MINIMUM_SIMILARITY` is gone from it.

## 13.3 `SearchQuery` value object: considered, deferred

`embedding_vector.py` ended with a comment anticipating a `SearchQuery` value
object "once its domain behavior becomes necessary". RFC-025 evaluated it and
deferred it, and the comment was rewritten to say so rather than left pointing
at an indefinite future that has now been examined.

Validation is one rule — reject blank text — and it lives in the use case. A
value object whose only job is carrying an already-checked string buys
nothing: every construction site would still be one call away from the single
place that validates. It earns its keep when there is real behavior to host
(normalization the repository must agree with, structured filters, a query the
model rewrites), and the validation moves with it then.

# 14. Alternatives considered

| alternative | why not |
| --- | --- |
| Score as a field on `Image` | The entity is frozen and compares by id; a per-query number on a per-image type creates two "equal" images with different data (§4.1) |
| Rescale similarity to [0, 1] | Destroys the distinction between unrelated (~0) and opposite (~-1), and the measured minimum is already negative (§9.1) |
| Return raw pgvector distance | Leaks "lower is better" and the existence of `<=>` into every layer above Infrastructure |
| Rank in Python | 100,000 × 512 floats per search, and it discards the index the schema already carries |
| `search_similar(query: str)` on the port | Puts the embedding model behind the repository; Domain would then depend on how text becomes a vector |
| Collection or path filter on the port | An Application concern invented to make tests isolatable (§8) |
| Clamp an out-of-range `limit` | Hides the disagreement in data the caller then reasons about (§13.1) |
| Dedicated test database | Right answer, wrong RFC — it changes how the entire suite reaches PostgreSQL (§10.1) |
| Sorting the hits again in the use case | Overrules the only component that saw the stored vectors |
| A `SearchQuery` value object now | One validation rule does not pay for a type (§13.3) |

# 15. Risks and future work

| risk | status |
| --- | --- |
| **HNSW recall is unvalidated.** At 45 rows the planner never uses the index (§7) | Accepted and stated. Needs the 100k benchmark (RFC-026), not a fake test |
| **The floors are a 25-query set.** One query is 4 points | Slack is two queries wide, and the failure modes that matter cost ten (§11.4) |
| **Negation does not work.** "not an artificial pond" retrieves the pond (§11.2) | A model property. Fine-tuning is RFC-023 §18 |
| **Cold start blows the latency target** by ~5 s once per process (§12) | Belongs to the HTTP-layer RFC, where warm-up has a place to live |
| **Test isolation locks the table** and assumes a single-process suite (§10.1) | True today; revisit before adopting `pytest-xdist` |
| **An HNSW scan can return fewer hits than `limit`**, and its top-K is approximate (§7.3) | Documented at the implementation that qualifies it; `ef_search` and vacuum policy belong to the scale RFC |
| Marian's leading `". "` on one translation (§11.5) | Recorded, not changed — RFC-023's component |

Future work, in rough order of value: a `/search` HTTP endpoint; the 100k
benchmark that would actually exercise HNSW and its `ef_search` tuning; a
minimum-similarity filter chosen from §9.1's distribution; collection scoping
once `Collections` exists; a dedicated test database; and domain fine-tuning,
which §11.2 suggests is where the remaining quality actually is.

# 16. Non-goals

Confirmed absent from the implementation:

- **HTTP `/search` endpoint.** There is no HTTP layer to put it in — the
  presentation package holds one-line placeholders and a `/health` route
- frontend, thumbnails, pagination, autocomplete
- fine-tuning, LoRA, checkpoint swaps, quantization, ONNX (RFC-023 §17)
- any new migration or index change (§5)
- HNSW tuning: `m`, `ef_construction`, `ef_search`
- a 100,000-image benchmark, and any pretence of index coverage at 45 rows (§7.1)
- `Collection`, `collection_id`, collection-scoped search (§8)
- `SearchHistory` (`ARCHITECTURE.md` §15)
- minimum-similarity filtering, reranking, hybrid search, cross-encoders,
  image-to-image search
- query batching — one query produces one embedding

# 17. Deliverables

**New**

| file | purpose |
| --- | --- |
| `backend/app/domain/value_objects/search_hit.py` | `SearchHit`, `SearchHits` |
| `backend/app/domain/exceptions/search_errors.py` | `EmptySearchQueryError`, `InvalidSearchLimitError`, `EmbeddingDimensionMismatchError` |
| `backend/tests/infrastructure/persistence/test_search_similar_contract.py` | The three-implementation contract (§10.2) |
| `backend/tests/dataset/test_semantic_search_e2e.py` | Real CLIP + PostgreSQL over the 25 queries (§10.5) |
| `experiments/rfc-025-semantic-search/measure_retrieval.py` | The measurement behind §9.1 and §11 |
| `experiments/rfc-025-semantic-search/planner_check.py` | The `EXPLAIN ANALYZE` sweep behind §7 |
| `docs/rfcs/rfc-025-semantic-search.md` | This document |

**Modified**

| file | change |
| --- | --- |
| `backend/app/domain/repositories/image_repository.py` | `search_similar()`; `from __future__ import annotations` |
| `backend/app/domain/value_objects/embedding_vector.py` | `SearchQuery` comment rewritten (§13.3) |
| `backend/app/domain/exceptions/__init__.py` | Three new exports |
| `backend/app/application/use_cases/search_images.py` | The real use case, `MAX_SEARCH_LIMIT`, injected default |
| `backend/app/infrastructure/persistence/postgres_image_repository.py` | `search_similar()`, dimension guard |
| `backend/app/infrastructure/persistence/in_memory_image_repository.py` | Stores embeddings; `search_similar()`; shared `cosine_search()` |
| `backend/app/infrastructure/database/models/image_model.py` | `EMBEDDING_DIMENSION` constant (§5) |
| `backend/app/infrastructure/config/settings.py` | `minimum_similarity` removed; `top_k_results` documented |
| `backend/app/presentation/dependencies/__init__.py` | Injects `settings.top_k_results` |
| `backend/tests/conftest.py` | `empty_db_session` fixture (§10.1) |
| `backend/tests/application/fakes.py` | Embedding storage, `seed_embedding()`, `search_similar()` |
| `backend/tests/application/test_search_images.py` | Rewritten: 15 use-case tests |
| `backend/tests/dataset/test_queries.py` | Dormant cosine loop removed (§10.4) |
| `backend/tests/domain/test_image_repository_port.py` | `search_similar` in the port contract |
| `backend/tests/domain/test_domain_exceptions.py` | The three new exceptions |
| `backend/tests/presentation/test_dependencies.py` | `top_k_results` wiring |
| `.env.example` | `MINIMUM_SIMILARITY` removed |

# 18. Validation

| check | result |
| --- | --- |
| `pytest` | **471 passed**, 56 deselected — from 429 passed + 1 xfailed at `HEAD` |
| `pytest -m slow` | **56 passed** (from 48), including the 8 end-to-end search tests |
| `black --check .` | clean, 126 files |
| `ruff check .` | clean |
| `mypy` | **5 errors in 4 files** — byte-identical to the pre-existing baseline, verified against `HEAD` in a scratch worktree; **0 in new or modified RFC-025 code** |
| `alembic heads` | `26058b9e1d9a (head)` — unchanged, no migration added |
| Application layer imports | no `app.infrastructure`, `sqlalchemy`, `fastapi`, `pydantic`, `alembic` |
| Development database | 3 pre-existing rows, unchanged — every test and measurement rolled back |
| Repository contract | 27 tests = 9 behaviors × 3 implementations, all green |
| Bake-off cross-check | pairwise identical to RFC-023 (§11.3) |
