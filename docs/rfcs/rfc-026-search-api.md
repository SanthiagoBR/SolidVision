# RFC-026 — Search API (HTTP end to end)

**Status:** Implemented (2026-08-23)
**Depends on:** RFC-022 (demo dataset), RFC-023 (CLIP adapter), RFC-024 (embedding pipeline), RFC-025 (semantic search)
**Migration:** none — `alembic heads` still reports `26058b9e1d9a`, see §5
**Measurement:** `experiments/rfc-026-search-api/measure_http_latency.py`,
output in `http_latency_run_output.log`

> **Draft convention, discharged.** Every number in this document marked
> `TBM` was *to be measured* during implementation and written back here
> afterwards. RFC-023 through RFC-025 all measured first and wrote the
> number down second; a draft that shipped with guessed numbers would
> break that discipline on the one RFC where it is easiest to break it,
> because the HTTP layer adds no new arithmetic and invites the
> assumption that RFC-025's numbers carry over unchanged. They mostly
> did. That is now a measurement rather than a deduction, and §19
> records every place where implementation disagreed with this draft.

---

# 1. Context

RFC-025 ends with a working search and no way to reach it:

> **§16, Non-goals:** *"HTTP `/search` endpoint. There is no HTTP layer to
> put it in — the presentation package holds one-line placeholders and a
> `/health` route."*
>
> **§15, Future work, first item:** *"a `/search` HTTP endpoint."*

So the scope of this RFC was set by the previous one, deliberately, rather
than chosen now. What exists today, verified against the tree rather than
recalled:

| piece | state |
| --- | --- |
| `SearchImagesUseCase` | real, 15 unit tests, validates query and limit |
| `ImageRepository.search_similar()` | real, 27 contract tests across 3 implementations |
| `ClipEmbeddingModel` | real, lazy, PT→EN, `a photo of {query}` |
| `get_search_images_use_case()` | **already wired**, injects `settings.top_k_results` |
| `get_embedding_model()` | **already cached**, `@lru_cache(maxsize=1)` |
| a route that calls any of it | **does not exist** |
| `presentation/api/v1/routers/collections.py` | a docstring |
| `presentation/schemas/image_schema.py` | a docstring |

The composition root is the part people expect to be missing here and it
is not: `backend/app/presentation/dependencies/__init__.py` already
composes the use case out of a real `PostgresImageRepository` and the real
CLIP adapter, and `tests/presentation/test_dependencies.py` already pins
that wiring — including that resolving the model downloads nothing.

**This RFC is therefore much smaller than "build the API layer."** It is a
router, a pair of response schemas, an exception handler, a startup hook,
and one bug (§7) that only becomes a bug under HTTP.

# 2. Problem

```
GET /api/v1/images/search?q=fish+ponds&limit=10
        ↓  FastAPI: parse, coerce, reject malformed input
   SearchImagesUseCase.execute(query, limit)
        ↓  (RFC-023) langdetect + Marian + template + CLIP text tower
        ↓  (RFC-025) pgvector <=> over vector(512), HNSW-eligible
   list[SearchHit]
        ↓  Presentation: map to DTOs
   200 {"query": ..., "limit": ..., "results": [...]}
```

The constraint that shapes every decision below, and the direct analogue
of RFC-025 §2's *"the ranking belongs to the database"*: **the route owns
nothing.** A correct implementation that reached for `SessionLocal`,
`ClipEmbeddingModel`, or a cosine anywhere inside `routers/images.py`
would pass every test in this RFC and would have dismantled the four
layers the previous three RFCs spent their whole scope keeping apart.

# 3. Decision

| decision | outcome |
| --- | --- |
| Verb and path | `GET /api/v1/images/search` (§4, §6) |
| Query parameters | `q` (required), `limit` (optional, defaults to `settings.top_k_results`) |
| Response body | `{query, limit, results: [{id, filename, similarity}]}` (§5) |
| `path` in the response | **Absent.** The server filesystem is not a public interface (§5.2) |
| Serving image bytes | **Not in this RFC.** `GET /images/{id}` is RFC-027 (§16) |
| Indexing over HTTP | **Not in this RFC.** The worker CLI stays the only entry point (§16) |
| Domain error → status | `EmptySearchQueryError`, `InvalidSearchLimitError` → **400**, via one app-level handler (§8) |
| Malformed/missing parameter → status | **422**, FastAPI's own, deliberately not flattened to 400 (§8.1) |
| Session lifetime | `Depends(get_db)`, one session per request, closed on the way out (§7) |
| Endpoint style | **`def`, not `async def`** — inference blocks (§9) |
| Model warm-up | `lifespan` hook, **opt-in** via `settings.warm_up_models` (§10) |
| `/health` | Stays at `/health`, unversioned, unchanged (§6.1) |
| Migration | None (§5) |

# 4. Architecture

The layer table from RFC-025 §4, extended by one row. The new row is the
only thing this RFC adds, and what it *does not* know is the whole point:

| layer | knows | does not know |
| --- | --- | --- |
| **Presentation (new)** | HTTP verbs, status codes, query-string parsing, JSON shape | that similarity is cosine, that a vector exists, that text is translated, that PostgreSQL is involved |
| **Application** | what makes a request usable — non-blank query, `limit` in range | how text becomes a vector, how a vector becomes a ranking |
| **Domain** | that a search returns ranked `SearchHit`s | SQL, pgvector, HNSW, CLIP, translation |
| **Infrastructure (AI)** | langdetect, Marian, the template, the text tower | that a search exists at all |
| **Infrastructure (persistence)** | `<=>`, `vector_cosine_ops`, distance → similarity | what a query said, or in what language |

The route body is expected to be roughly four lines. That is the same
observation RFC-025 §4 made about the use case being nine, and it means
the same thing: the pipeline crosses five specialist components, and the
place it is *exposed* understands none of them.

## 4.1 What the route is forbidden to do

Stated as a list because it is also a test (§11.4):

- import `sqlalchemy`, `SessionLocal`, or `EngineInstance`
- import `torch`, `transformers`, `PIL`, `langdetect`, or `ClipEmbeddingModel`
- name a model, a template, an operator, or a distance
- re-sort, filter, threshold, or truncate `SearchHit`s
- construct `SearchImagesUseCase` itself rather than receiving it

`tests/test_ai_layer_boundaries.py` already enforces the AI half of this
for Domain and Application by walking the AST. This RFC extends the same
walker to `app/presentation/api/`, because `AI_Context.md`'s dependency
rules list **Presentation → Database** and **Presentation → AI Models** as
forbidden and nothing currently checks either.

# 5. The HTTP contract

## 5.1 Request

```
GET /api/v1/images/search?q=fish%20ponds&limit=10
```

| parameter | type | required | notes |
| --- | --- | --- | --- |
| `q` | string | yes | The user's query, in any language the adapter handles |
| `limit` | integer | no | Omitted ⇒ `settings.top_k_results` (default 10) |

`limit` is **not** given `ge`/`le` constraints in the FastAPI signature,
and that is deliberate rather than an oversight — see §8.2.

## 5.2 Response — 200

```json
{
  "query": "fish ponds",
  "limit": 10,
  "results": [
    {
      "id": "3f2a8c14-...",
      "filename": "fish_ponds_02",
      "similarity": 0.3255
    }
  ]
}
```

**`query` echoes the user's input verbatim, never the prompt.** RFC-025
§11.5 recorded that Marian emits one sentence with a leading `". "`, so
the string actually handed to CLIP for that query is
`"a photo of . a rural property with a lake"`. Echoing the *prompt* would
publish an internal artifact of the translation path as though it were
part of the contract, and would make the response change the day RFC-023's
template changes. The client gets back what it sent.

**`filename` is the domain's `filename`, which carries no extension.**
`FilesystemImageProvider` builds it from `path.stem` and keeps the
extension in a separate field, so `fish_ponds_02.jpg` on disk is
`filename="fish_ponds_02"`, `extension="jpg"` in the entity. This draft's
example said `fish_ponds_02.jpg` and was wrong about the codebase it was
describing. The response publishes the field as it is rather than
rejoining the two: a rejoined name is a value Presentation invented, and
this is the layer that is supposed to invent nothing. A client that needs
the format will have it from the `Content-Type` of RFC-027's
`GET /images/{id}`.

**`limit` echoes the effective value, not the requested one.** It is
absent from the request more often than not, and a caller that receives
three results has no way to tell "there were only three images" from "the
default is smaller than I assumed" without it. It costs one field.

**No `path`.** `Image` carries `path: ImagePath` and
`PostgresImageRepository` returns it, so this is a choice to drop
information on the way out, not an absence of it. Two reasons, and the
second is the load-bearing one:

1. It publishes the server's filesystem layout — `C:\Users\...` on this
   machine — to every client.
2. It is the wrong identifier to build a client on. The filesystem is the
   source of truth (`AI_Context.md`), files move, and RFC-024's
   `compute_image_id` derives the id from the path — so a client that
   stored a path would hold something that changes *and* something the
   next RFC's `GET /images/{id}` cannot accept.

**`similarity` is passed through unrounded and unclamped**, in `[-1, 1]`.
RFC-025 §9.1 measured the real distribution — minimum **-0.1183**, best
hit per query averaging **+0.3003** — and §14 there rejected rescaling to
`[0, 1]` because it destroys the distinction between unrelated (~0) and
opposite (~-1). Presentation is exactly the layer that would be tempted to
"fix" the number for a UI, and it is exactly the layer with the least
information about what the number means. A client that wants a percentage
can compute one, having read the distribution.

**No migration.** Search is a read (RFC-025 §5) and HTTP does not change
that. `alembic heads` must still report `26058b9e1d9a` as the single head
after this RFC, and §18 checks it.

## 5.3 Errors

| case | status | body |
| --- | --- | --- |
| `q` present but blank or whitespace | **400** | `{"detail": "Search query cannot be empty or only whitespace."}` |
| `limit=0`, `limit=-1`, `limit=101` | **400** | `{"detail": "Search limit must be at most 100, got 101."}` |
| `q` missing entirely | **422** | FastAPI's validation body |
| `limit=abc` | **422** | FastAPI's validation body |

# 6. Route placement, and a fork the tree already contains

The repository currently holds **two** route packages:

```
app/presentation/routes/           health.py        → registered, unversioned
app/presentation/api/v1/routers/   collections.py   → a docstring, unregistered
```

`AI_Context.md` documents `presentation/api/v1/routers/` as the convention
("Routers — one resource per router"), so the second is where the project
already said routes go; the first is where the only real route actually
is. This RFC has to pick one, because adding search to whichever is
nearest is how a codebase ends up with both forever.

**Decision: search goes in `app/presentation/api/v1/routers/images.py`,
mounted under `/api/v1`.** It follows the documented convention, and the
`v1` prefix is worth having before there are clients rather than after —
a search response is exactly the shape that grows a `path`, a thumbnail
URL, or a `total` once a UI exists (RFC-027), and versioning after
publication is a migration rather than a decision.

## 6.1 `/health` stays where it is

Not moved, not versioned, not touched. A health probe is infrastructure,
not a resource in the product's API: it is what a container orchestrator
or a load balancer calls, those are configured with a fixed path, and
`v2` of the search API will not mean a `v2` of "is the database
reachable". Leaving it unversioned is the common convention and it is also
the smaller diff — `tests/presentation/test_health.py` keeps passing
untouched, which is what "do not refactor unrelated modules"
(`AI_Context.md`) asks for.

The residue is honest and worth writing down: after this RFC the tree has
one unversioned infrastructure route and one versioned resource router,
which is a defensible arrangement, and `presentation/routes/` should not
acquire a second resource route later.

## 6.2 Dead files, removed

Both found while reading the tree for this draft, both unrelated to
search except that they are in the package it lands in:

**`app/presentation/api.py`** — a "compatibility shim" whose entire body
is `from app.presentation.api import app`. It sits beside the *package*
`app/presentation/api/`, which shadows it: Python resolves
`app.presentation.api` to the package, so this module is never imported,
and if it ever were it would import itself. It is not a shim, it is a file
that cannot run.

**`app/presentation/dependencies/dependencies.py`** — a one-line docstring
placeholder living next to the real `dependencies/__init__.py`, i.e.
exactly the file a reader looking for the wiring would open first, and the
only one of the two that contains nothing.

Removing both is in scope because this RFC is the first work in years to
touch that package, and leaving them means the next reader pays the same
tax. Neither is imported anywhere — §18 verifies that before deletion
rather than after.

A third went with them, decided during implementation:
`app/presentation/schemas/image_schema.py`, which §17 left as "filled, or
removed". `search_schema.py` covers everything search returns, and an
image schema belongs to RFC-027, which will have a payload to describe.
`collection_schema.py` is deliberately left where it is: Collections is
not on this RFC's path, and removing a placeholder merely because it is
nearby is the kind of drive-by refactor `AI_Context.md` asks against.

# 7. The session bug that HTTP creates

This is the one real defect this RFC has to fix, and it is invisible
today.

```python
def get_image_repository() -> ImageRepository:
    return PostgresImageRepository(SessionLocal())
```

Nothing closes that session. Ever. There is no `try/finally`, no
generator, no context manager — and `get_db()`, which does all three
correctly, sits unused in `infrastructure/persistence/session.py` with the
docstring *"Provide a database session for future FastAPI dependencies."*
The future in that sentence is this RFC.

**Why it has been harmless.** `get_image_repository()` has exactly one
kind of caller today: the tests, plus the two sibling providers that are
themselves only called by tests. Verified rather than assumed — a grep for
`get_image_repository`, `get_index_image_use_case`, and
`get_search_images_use_case` across `backend/` returns
`tests/presentation/test_dependencies.py` and `dependencies/__init__.py`
and nothing else.

The indexing worker notably does **not** use it. `IndexingWorker.main()`
opens its own session and closes it properly:

```python
session = SessionLocal()
try:
    worker = IndexingWorker(...)
    worker.run()
finally:
    session.close()
```

So the correct lifetime already exists in this codebase, twice — in
`get_db()` and in the worker — and the provider is the one place that
skipped it.

**What changes under HTTP.** Every request calls the provider, so every
request opens a session, and a session that has executed a `SELECT` holds
a pooled connection inside an open transaction until something closes it.
Nothing does, so the connection is returned only when the `Session` is
garbage-collected and SQLAlchemy's pool finalizer runs. Three consequences,
in ascending order of how hard they are to debug:

1. **Pool exhaustion under concurrency.** SQLAlchemy's default pool is 5
   connections with 10 overflow. Requests arriving faster than the garbage
   collector reclaims sessions will queue and then time out.
2. **The failure is non-deterministic**, because it depends on GC timing.
   It will not reproduce in a single-request manual test, will not
   reproduce in the unit suite, and will surface as an intermittent
   timeout under load — the worst available combination.
3. **It blocks autovacuum, and RFC-025 already measured what that costs.**
   An unclosed read transaction is an `idle in transaction` backend, and
   PostgreSQL cannot vacuum tuples still visible to one. RFC-025 §7.3
   documents, from an accident rather than a theory, that dead index
   entries make an HNSW scan return **1 of 3 live rows** — the contract
   tests failed on exactly this and `VACUUM ANALYZE` fixed them. A
   long-lived idle transaction is the mechanism that keeps dead tuples
   alive at scale. The connection leak and the recall degradation are the
   same bug seen from two ends.

**The fix.** Make the providers FastAPI dependencies over `get_db()`:

```python
def get_image_repository(session: Session = Depends(get_db)) -> ImageRepository:
    return PostgresImageRepository(session)

def get_search_images_use_case(
    repository: ImageRepository = Depends(get_image_repository),
    embedding_model: EmbeddingModelPort = Depends(get_embedding_model),
) -> SearchImagesUseCase:
    return SearchImagesUseCase(
        repository=repository,
        embedding_model=embedding_model,
        default_limit=settings.top_k_results,
    )
```

FastAPI then closes the session when the response is sent, and
`app.dependency_overrides` gets a seam at every level, which §11 uses.

`get_index_image_use_case()` had to move with them, which this draft did
not say. It has no HTTP route and never will — indexing is the worker's
job — but it was composing itself out of `get_image_repository()` by
direct call, so leaving it alone would have kept a second, uninjected
copy of the leak alive behind a provider that still looked wired.

**The cost, stated rather than discovered later.** These providers stop
being plain callables. `tests/presentation/test_dependencies.py` calls
`get_image_repository()` and `get_search_images_use_case()` directly, and
with `Depends(...)` defaults those calls would receive `Depends` objects
instead of a session. Those tests must be updated — which is the point of
having them, but it means the diff reaches a file that looks unrelated to
"add an endpoint". The alternative — keeping the plain functions and
opening a second, uninjected session per request — is rejected in §14.

## 7.1 `get_embedding_model()` must stay directly callable

A constraint that is easy to miss and would break a CLI nobody was
thinking about: `IndexingWorker.main()` **does** import from this module —

```python
from app.presentation.dependencies import get_embedding_model
...
embedding_model=get_embedding_model(),
```

— and calls it as a plain zero-argument function. So the rule is narrow
but firm: `get_image_repository()` and `get_search_images_use_case()` may
grow `Depends(...)` parameters, because only tests call them.
`get_embedding_model()` may **not**, because a non-HTTP caller depends on
calling it directly.

This costs nothing, because a zero-argument provider already works as a
FastAPI dependency — `Depends(get_embedding_model)` and
`get_embedding_model()` are both valid against the same signature. It is
recorded because the natural instinct while converting the module is to
convert all of it, and the worker would then fail at runtime with an
`lru_cache`d `Depends` object standing in for a model, far from the edit
that caused it. §18 runs the worker to check.

Worth noting in passing, and explicitly *not* fixed here: Infrastructure
importing Presentation is an inverted dependency. `main()`'s docstring
shows the authors were aware — the import is function-local specifically
so that importing `IndexingWorker` does not drag Presentation and CLIP
into an Infrastructure import graph. Moving the composition root out of
`presentation/dependencies/` is a real cleanup and a different RFC's;
this one must not quietly change where the worker gets its model.

# 8. Errors: one handler, and a boundary worth defending

## 8.1 400 and 422 are different failures, and stay different

`EmptySearchQueryError` and `InvalidSearchLimitError` both derive from
`DomainError`, so one handler covers both and every domain error a future
use case adds:

```python
@app.exception_handler(DomainError)
def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})
```

Registered on the app, not caught in the route, so the route keeps the
shape §4 requires — no `try/except` around the use case call.

That leaves a boundary the contract must state, because it is the kind of
thing an implementer will "tidy up" without noticing it is a decision:

| failure | who rejects it | status |
| --- | --- | --- |
| `?q=` — a query that is syntactically fine and semantically empty | `SearchImagesUseCase` | 400 |
| `?limit=101` — an integer outside application policy | `SearchImagesUseCase` | 400 |
| `?limit=abc` — not an integer | FastAPI/pydantic, before the route runs | 422 |
| no `q` at all — the request does not match the endpoint | FastAPI/pydantic | 422 |

The split is real: 422 means *this is not a well-formed request to this
endpoint*, 400 means *this request was understood and refused*. Flattening
422 into 400 for tidiness would erase that, and would put Presentation in
the business of re-describing failures Application never saw.

## 8.2 Why `limit` carries no `ge`/`le` in the signature

FastAPI would happily enforce `Query(ge=1, le=100)`, and it is the obvious
thing to write. It is rejected, for the reason RFC-025 §13.1 gave when it
chose to raise rather than clamp: **the rule already exists, once, in the
Application layer.**

`MAX_SEARCH_LIMIT = 100` lives in `search_images.py` with a comment
explaining that it is *"Application policy rather than a repository or
database limit."* Copying `100` into a route decorator creates a second
copy of a policy that must not drift, and drift is silent — the day
someone raises `MAX_SEARCH_LIMIT` to 200, the endpoint keeps returning 422
at 101 and the use-case tests keep passing. RFC-025 §5 made this exact
call about the literal `512` and named the constant instead: *"two copies
of a physical-schema fact are one copy too many."*

The cost is that out-of-range limits come back as 400 rather than 422.
That is the correct status anyway (§8.1): `limit=101` *is* a well-formed
request that policy refuses.

# 9. `def`, not `async def`

The endpoint is a synchronous `def`. This is a decision, not a default.

`encode_text()` is blocking CPU work — RFC-025 §12 measured **mean 89.7 ms,
median 87.5 ms, max 128.7 ms** for English on CPU, and ~400 ms for
Portuguese including translation. `search_similar()` is a blocking DBAPI
call through psycopg. Neither yields.

FastAPI runs a `def` endpoint in a threadpool and an `async def` endpoint
on the event loop. Declaring this route `async def` would block the event
loop for ~90–400 ms per search, stalling *every* concurrent request
including `/health`. The rule is unusually clean here: **the entire call
chain below this route is synchronous, so the route must be synchronous
too.**

## 9.1 The threadpool creates a race the single-threaded path never had

Running in a threadpool means concurrent requests share one
`ClipEmbeddingModel` — `get_embedding_model()` is `@lru_cache(maxsize=1)`,
which is the correct design and stays. But its lazy load is a
check-then-act:

```python
if self._model is None or self._processor is None:
    ...
    self._model = model.to(self._device)
```

Two requests arriving before the first load completes can both evaluate
`self._model is None` as true and both load the checkpoint — roughly 5 s
and several hundred MB each, on a cold process, concurrently. `lru_cache`
does not prevent this: it makes them share the *adapter*, and the race is
inside the adapter.

Two candidate fixes, and this RFC prefers the first:

1. **Warm up at startup (§10).** If the model is loaded before the server
   accepts its first request, no request ever observes `_model is None`,
   and the race has no window. This costs nothing at request time.
2. A lock in `_ensure_loaded()`. Correct, but it is a change to RFC-023's
   component to solve a problem introduced by RFC-026's threadpool, and it
   puts a lock on the hot path of every encode for the life of the
   process.

**The honest caveat:** option 1 closes the window only when warm-up is
enabled, and §10 makes warm-up opt-in. With `WARM_UP_MODELS=false` — the
default — the race is real but narrow: it requires two requests within the
first ~5 s of a cold process, and its worst outcome is duplicated work and
memory, not a wrong answer. If that is judged unacceptable, the lock is
the answer and it belongs in a follow-up to RFC-023, not smuggled in here.
This is written down so the choice is visible rather than discovered.

# 10. Warm-up, and the trap in the obvious implementation

RFC-025 §12 assigned this work to this RFC by name:

> *"The first query of a process pays ~4.95 s to load the CLIP checkpoint,
> and the first Portuguese query pays a further ~4.20 s for the Marian
> translator. Both blow the 1-second target once per process. […] the fix
> — warming the model at startup — belongs to whichever RFC introduces the
> HTTP layer, where 'startup' first means something."*

So: a `lifespan` handler that resolves `get_embedding_model()` and encodes
one throwaway string, before the server is ready.

**The throwaway string is Portuguese, and that is the whole point.**
RFC-025 measured *two* cold starts and this draft quoted both, then
specified a warm-up that closes one. `encode_text()` only loads Marian
when the detector calls the input Portuguese, so an English warm-up
leaves the ~4.20 s translator load in place for the first Brazilian user
of every process — in a product whose users write Portuguese. One
Portuguese sentence walks detect → translate → template → encode and
loads both models. It is a *sentence* for the reason RFC-023 §7.1
measured: `langdetect` needs several words, so a short string would be
read as another language, skip translation, and warm only CLIP while
looking exactly like a warm-up that worked.
`test_the_warm_up_query_actually_reaches_the_translator` pins it.

Use `lifespan`, not `@app.on_event("startup")` — the latter is deprecated
in current FastAPI, and this is the first startup hook the project has, so
there is no existing style to match and no reason to adopt the deprecated
one.

## 10.1 The trap

**A warm-up that runs unconditionally breaks the offline test suite.**

`tests/presentation/test_health.py` opens the app as
`with TestClient(app) as test_client:`, and the `with` form is precisely
what triggers startup events. An unconditional warm-up would therefore
download and load CLIP during a plain `pytest` — violating RFC-023 §15's
rule that the fast suite never reaches Hugging Face, in a test file that
has nothing to do with search, for a route that does not use the model.

This is worth the space because it is a silent trap: the warm-up code
would look right, the search tests would pass, and the damage would appear
as `test_health.py` suddenly taking 5 s and needing a network.

## 10.2 The decision: opt-in, defaulting to off

```python
warm_up_models: bool = Field(
    default=False,
    description="Load the embedding model at API startup instead of on first request",
)
```

Default `False`, with `WARM_UP_MODELS=true` in `.env.example` for anyone
running the API for real.

The default is chosen the way RFC-024 §12 chose to make `--root` required
with no default — *"so the command can never begin indexing a real photo
collection nobody pointed it at."* Same principle: **the safe default is
the one that cannot download 600 MB into a process that never asked for
it.** A test run, a CI job, an `import app.presentation.api` in a
developer's REPL — none of those want a checkpoint, and all of them would
get one under `default=True`.

The cost is real and stated plainly: **out of the box, the API does not
meet the latency target on its first query.** It pays RFC-025's ~5 s cold
start, exactly as documented there. Turning warm-up on is one line in
`.env`, and §18 measures both paths rather than asserting the setting
works.

**A warm-up that fails is logged, not fatal.** The draft did not say,
and the choice is not obvious. Aborting startup would take `/health` down
along with search — the probe an operator would use to diagnose exactly
this — and would turn a transient Hugging Face outage into a crash loop.
So the exception is logged at `WARNING` with its traceback and the server
starts anyway; the lazy path is untouched, so the first query retries the
load and fails visibly if it still cannot. The cost is that a
misconfiguration surfaces as one warning line rather than a refusal to
start, which is what the line is for.
`test_a_failed_warm_up_does_not_stop_the_server` pins it.

**Measured** (this machine, CPU, warm Hugging Face disk cache):

| | startup |
| --- | --- |
| `WARM_UP_MODELS=false` | **0.00 s** |
| `WARM_UP_MODELS=true` | **8.37 s** |

Warm-up on misses `ARCHITECTURE.md` §22's 5 s startup target by 3.4 s,
and this draft predicted it would. The number is not a surprise and not a
defect: it is RFC-025's two cold starts (~4.95 s CLIP, ~4.20 s Marian)
moved off the first two queries and onto startup, where they are paid
once by nobody waiting for a response. The reconciliation §10 promised is
that the 5 s target was written for a process that loads no models, and a
process that loads two cannot meet it — while what a user actually
experiences, the first search, drops from ~5 s to ~0.13 s. Either the
target bounds a process that warms lazily, in which case warm-up is out
of its scope, or it is meant to bound the whole boot, in which case it
needs a number a checkpoint fits inside. That is a decision for whoever
owns §22, recorded here rather than quietly omitted.

# 11. Testing

Three levels, and the reason there are three is that each one can fail
while the others pass.

## 11.1 Level 1 — route unit tests (fast, always run)

`TestClient` + `app.dependency_overrides[get_search_images_use_case]`
returning a stub. No database, no model, no network.

| test | pins |
| --- | --- |
| 200 with the documented body for a normal query | §5.2 |
| `query` echoes the raw input, not a prompt | §5.2 |
| `limit` omitted ⇒ response echoes `settings.top_k_results` | §5.2 |
| the route passes `q` and `limit` through **unmodified** | §4 |
| results appear in the repository's order, unsorted by the route | RFC-025 §4 |
| `similarity` survives as a float, negative values included | §5.2 |
| `path` is **absent** from every result object | §5.2 |
| `?q=` ⇒ 400 with the domain message | §8.1 |
| `?limit=0` / `?limit=101` ⇒ 400 | §8.1 |
| missing `q` ⇒ 422 | §8.1 |
| `?limit=abc` ⇒ 422 | §8.1 |
| empty result set ⇒ 200 with `"results": []`, not 404 | — |

The last one deserves its line: "no image matched" is a successful search
with nothing in it, not a missing resource. 404 would mean the *endpoint*
was not found.

## 11.2 Level 2 — API integration (real Postgres, fake model)

`TestClient` + the real dependency graph + `empty_db_session` +
`FakeEmbeddingModel` substituted for the CLIP adapter.

This is the level that proves the wiring, and the reason it exists
separately from level 3 is that it exercises **dependency injection,
session lifetime, the repository, pgvector, and JSON serialization**
without paying ~5 s for a checkpoint. `FakeEmbeddingModel` is suitable for
exactly this: it is deterministic across processes (SHA-256 in counter
mode, not `hash()`), produces mean-centred unit vectors in
`settings.embedding_dimension`, and its own docstring records that its
geometry was fixed in RFC-022 §7.4 so that similarity search over it is
not trivially degenerate. It has no semantics — which is fine, because
semantics is level 3's job.

| test | pins |
| --- | --- |
| a request over seeded rows returns them ranked, best first | the whole chain |
| the row with the NULL embedding never appears | RFC-025 §6 |
| `limit` truncates the real result set | §5.1 |
| ids in the response are the ids in the database | §5.2 |
| **the session is closed when the response is sent** | §7 |

That last test is the one this level exists for, and the mechanism is
the one the draft guessed at: `QueuePool.checkedout()` before and after
five sequential requests, with `get_db` deliberately *not* overridden, so
the real provider's `finally: session.close()` is what is being measured.
The `isinstance(pool, QueuePool)` guard is load-bearing — under
`NullPool` every reading would be zero and the test would pass by
construction.

**Verified against the bug rather than against the fix.** Restoring the
old one-line `PostgresImageRepository(SessionLocal())` body was tried,
and it fails **four of the five** tests at this level while leaving all
34 other fast presentation tests green. The four fail for two different
reasons, and the pair is worth separating:

* the pool test fails on the leak itself — connections never come back;
* three others fail because the old provider ignores its injected session
  entirely, so a request no longer reads the transaction the fixture
  seeded and ranks whatever the development database happens to hold.

The second reason is the one that explains why the bug stayed invisible
for so long: nothing before this RFC ever asked a *request* to see a
row a *test* had written, because nothing before this RFC issued a
request. Levels 1 and 3 stay green throughout, exactly as they should —
level 1 stubs the use case and level 3 overrides the repository, so
neither has an opinion about where a session comes from. That is the
check the draft asked for in §7: a test that merely asserts a 200 sails
straight past the leak.

## 11.3 Level 3 — true end to end (`slow`)

Real CLIP, real PostgreSQL, real pgvector, over HTTP. Marked `slow` and
deselected by the default `addopts`, following RFC-023 §12.1.

**Deliberately small.** `tests/dataset/test_semantic_search_e2e.py`
already runs all 25 ground-truth queries through the real stack with
measured regression floors (Recall@5 ≥ 76%, contamination ≤ 20%, strict
top-1 ≥ 56%, pairwise ≥ 72%). Re-running quality metrics through HTTP
would measure the model a second time and call it an API test. The
question this level answers is narrower:

> Does a real HTTP request reach real CLIP and real pgvector and come back
> as correct JSON?

| test | assertion |
| --- | --- |
| an English query returns a relevant image in the top 5 | reuses existing ground truth |
| a full-sentence Portuguese query returns overlapping results | proves translation is on the *HTTP* path |

The Portuguese test is full-sentence for the reason RFC-023 §7.1 measured:
`langdetect` calls `fazenda` Turkish and `lago` Tagalog, so a one-word
query reaches CLIP untranslated and would test the raw-PT path while
claiming to test translation.

**No new fixture corpus.** The RFC-022 demo corpus, its manifest, its
`queries.json`, and the module-scoped indexing fixture already exist and
are already isolated correctly. A second four-image dataset would be a
parallel ground truth to maintain, drifting from the first, to answer a
question the first can answer.

## 11.4 The boundary test

Extend `tests/test_ai_layer_boundaries.py`'s AST walk to
`app/presentation/api/`, asserting no import of `sqlalchemy`, `psycopg`,
`pgvector`, `torch`, `transformers`, `PIL`, `langdetect`, or any module
whose name contains `clip`.

`AI_Context.md` lists **Presentation → Database** and **Presentation → AI
Models** as forbidden, and nothing checks either today — the rule has been
trivially satisfied only because Presentation was empty. This RFC is the
first commit that could violate it, so it is the commit that should make
it enforceable. Note the deliberate exemption: `dependencies/` **must**
import both, because composing Infrastructure is what a composition root
is for. The walk covers the routers, not the wiring.

# 12. Performance

Measured by `experiments/rfc-026-search-api/measure_http_latency.py`: the
45-image RFC-022 corpus indexed inside a rolled-back transaction, a real
uvicorn server on a real socket, and every query timed twice — once
through `SearchImagesUseCase.execute()` and once through HTTP — so the
difference is the answer. `TestClient` was deliberately not used for
timing: it drives the ASGI app in-process and would have measured the
layer with its transport removed, which is the part being asked about.
20 repeats per row, warm.

| | mean | median | p95 | max |
| --- | --- | --- | --- | --- |
| English, use case direct | 130.8 ms | 124.2 ms | 168.2 ms | 176.3 ms |
| **English, over HTTP** | **131.1 ms** | 127.8 ms | 152.3 ms | 155.6 ms |
| Portuguese, use case direct | 393.1 ms | 375.1 ms | 527.3 ms | 544.3 ms |
| **Portuguese, over HTTP** | **381.5 ms** | 371.0 ms | 430.2 ms | 472.1 ms |

**The HTTP layer costs +0.3 ms on English and −11.6 ms on Portuguese.**
The negative number is the useful one: it is not a speed-up, it is proof
that the delta is smaller than the run-to-run noise of the thing being
measured. Routing, dependency resolution, `vector(512)` ranking, and JSON
serialization together are lost inside the variance of one CLIP forward
pass. Nothing here is worth optimizing, and the reason is that there was
never any arithmetic in this layer to optimize.

Against the targets:

| target (`ARCHITECTURE.md` §22) | RFC-025 measured | RFC-026 over HTTP |
| --- | --- | --- |
| Search latency < 1 s | ~90 ms warm English (use case) | **131 ms**, met |
| | ~400 ms warm Portuguese (use case) | **382 ms**, met |
| | 0.36 ms database at 45 rows | unchanged, no query change |
| Startup < 5 s | models load lazily | **0.00 s** off / **8.37 s** on (§10.2) |
| Cold first request | ~4.95 s CLIP + ~4.20 s Marian | removed by warm-up; ~5 s without |

Two honest caveats about the absolute numbers, neither of which touches
the delta. English came out at 131 ms here against RFC-025's 89.7 ms on
the same machine and the same checkpoint; the run was not made on an idle
laptop, and this experiment never claimed to re-measure the text tower.
And the Portuguese p95 spread (430–544 ms) is Marian's generation length
varying, not the API.

**No new latency target is set.** RFC-025's §12 numbers were measured on
this machine, on CPU, and a target invented here without measuring the
real deployment would be exactly what the GPT review of this plan warned
against.

# 13. Configuration

| setting | change |
| --- | --- |
| `top_k_results` | unchanged. Already the injected default (RFC-025 §13.1); the endpoint now exposes it as the effective `limit` |
| `warm_up_models` | **new**, `bool`, default `False` (§10.2) |
| `MAX_SEARCH_LIMIT` | unchanged, stays in `search_images.py`, not duplicated into the route (§8.2) |

No new AI, database, or ranking configuration. Any pressure to add
`SEARCH_DEFAULT_LIMIT`, `SEARCH_MAX_LIMIT`, or an API-side similarity
threshold is a sign the route is acquiring policy, and belongs in §16.

# 14. Alternatives considered

| alternative | why not |
| --- | --- |
| `POST /images/search` with a JSON body | Search is a read: cacheable, linkable, idempotent, expressible as a URL a user can bookmark and a log can record. A body buys nothing at one string and one integer |
| Return `path` in the response | Publishes the server filesystem and hands clients an identifier that changes when a file moves (§5.2) |
| Serve image bytes in this RFC | Needs path-traversal defence, content-type negotiation, range requests, caching, and a thumbnail decision. That is RFC-027, and bundling it would make the E2E RFC the largest so far |
| `POST /index` alongside search | Indexing is the worker's job (`AI_Context.md`: *"Indexing is executed by the Worker, never by FastAPI"*). Over HTTP it additionally needs auth, uploads, job state, progress, cancellation, and path validation |
| `Query(ge=1, le=100)` on `limit` | Duplicates `MAX_SEARCH_LIMIT` into a second layer where it will silently drift (§8.2) |
| `async def` endpoint | Blocks the event loop for 90–400 ms per search; the whole chain below is synchronous (§9) |
| Keep `get_image_repository()` a plain function and open a session per request inside the route | The route would then import `SessionLocal`, violating §4.1, and would still have to close it by hand |
| Catch domain errors with `try/except` in the route | Puts error policy in every route instead of once on the app, and grows a branch per exception type (§8.1) |
| Flatten 422 into 400 | Erases the difference between a malformed request and a refused one (§8.1) |
| Round or rescale `similarity` for display | Presentation has the least information about what the score means, and RFC-025 §14 already rejected rescaling |
| Warm up unconditionally at startup | Downloads CLIP during a plain `pytest`, via `test_health.py` (§10.1) |
| A dedicated four-image E2E fixture corpus | A second ground truth to maintain, answering a question the RFC-022 corpus already answers (§11.3) |
| Re-measure Recall@5 through HTTP | Measures the model twice and calls the second one an API test (§11.3) |
| A frontend in this RFC | Not a backend concern, and it would hide whether the API is usable by anything else. RFC-028 |

# 15. Risks and future work

| risk | status |
| --- | --- |
| **Cold start still blows the latency target** when warm-up is off, which is the default (§10.2) | Accepted and documented; one line in `.env` turns it on |
| **Warm-up on misses the 5 s startup target**, measured at 8.37 s in-process and 11.23 s in a fresh server (§10.2, §18.1) | Accepted. It is RFC-025's two model loads moved off the first queries and onto boot. `ARCHITECTURE.md` §22 needs to say whether its 5 s bounds a process that loads models |
| **The lazy-load race** is closed by warm-up but open without it (§9.1) | Narrow window, no wrong answers; the lock belongs to an RFC-023 follow-up |
| **No auth.** The endpoint is open to anyone who can reach the port | Explicit non-goal (§16). Local-first product, no multi-user model exists yet |
| **No rate limiting**, and each request costs ~90–400 ms of CPU inference | Real DoS surface the moment this is exposed beyond localhost. Belongs with auth |
| **No pagination beyond `limit`.** No offset, no cursor, no total | Deliberate. A `total` over a top-K similarity query is a different query, and offset paging over an ANN index is not well defined |
| **HNSW recall is still unvalidated** (RFC-025 §7.1) | Unchanged by this RFC; still needs the 100k benchmark |
| **Test isolation still locks the table** and assumes a single-process suite (RFC-025 §10.1) | Unchanged; revisit before `pytest-xdist` |
| **Two route packages** now coexist by decision rather than by accident (§6.1) | Documented; `presentation/routes/` takes no further resource routes |

Future work, in rough order of value: `GET /images/{id}` and thumbnails
(RFC-027); a search UI (RFC-028); the 100k benchmark that would actually
exercise HNSW and `ef_search`; auth and rate limiting; a minimum-similarity
filter chosen from RFC-025 §9.1's distribution; collection-scoped search
once `Collections` exists.

# 16. Non-goals

Confirmed out of scope, and each one is a decision rather than an
oversight:

**Not touching what previous RFCs settled**

- the embedding model, checkpoint, dimension, template, or translation (RFC-023)
- the indexing pipeline, batching, hashing, or bulk writes (RFC-024)
- the ranking: the operator, the tie-break, the score range, HNSW tuning (RFC-025)
- any new migration, column, or index (§5)
- re-measuring retrieval quality (§11.3)

**Not building yet**

- serving image bytes, `GET /images/{id}`, thumbnails, CDN, static mounts
- any frontend, UI, or gallery
- indexing over HTTP, uploads, job queues, progress, cancellation
- authentication, authorization, multi-user, API keys, rate limiting
- pagination beyond `limit`: offset, cursors, `total`, `has_more`
- image-to-image search, hybrid search, full-text, reranking, cross-encoders
- metadata filters: date, location, extension, collection
- `SearchHistory`, saved searches, autocomplete, query suggestions
- caching of query embeddings or result sets
- WebSockets, streaming, async search, batch query endpoints
- OpenTelemetry, metrics endpoints, distributed tracing
- containerizing the API, deployment, reverse proxy, TLS

## 16.1 Observability: the minimum, and its limit

Two log lines, at `INFO`, through the existing `get_logger`:

```
search requested: query_length=11, limit=10
search completed: results=5, elapsed_ms=94
```

**The query text itself is not logged, and the embedding never is.**
Query length is enough to correlate a slow request with a long query;
the text is a user's search history written to a file that outlives the
request, and this is a local-first product whose whole premise is that
the user's data stays theirs. Logging it is a product and privacy
decision, not a debugging convenience — if it is wanted, it should be a
setting with a default of off, argued for in its own RFC.

# 17. Deliverables

As shipped. Three files differ from what the draft listed, and each is
marked.

**New**

| file | purpose |
| --- | --- |
| `backend/app/presentation/api/v1/routers/images.py` | The search route (§6) |
| `backend/app/presentation/schemas/search_schema.py` | `SearchResultSchema`, `SearchResponseSchema` (§5.2) |
| `backend/app/presentation/error_handlers.py` | `DomainError` → 400 (§8) |
| `backend/tests/presentation/test_search_route.py` | Level 1 (§11.1) |
| `backend/tests/presentation/test_search_api_integration.py` | Level 2 (§11.2) |
| `backend/tests/presentation/test_search_api_e2e.py` | Level 3, `slow` (§11.3) |
| `backend/tests/presentation/test_api_startup.py` | **Not in the draft.** Warm-up is a decision with three branches — off, on, failed — and §10.1 calls it a silent trap. A trap nothing tests is a trap |
| `experiments/rfc-026-search-api/measure_http_latency.py` | The measurement behind §12 |
| `experiments/rfc-026-search-api/http_latency_run_output.log` | Its output, kept the way RFC-023 and RFC-025 kept theirs |
| `docs/rfcs/rfc-026-search-api.md` | This document |

**Modified**

| file | change |
| --- | --- |
| `backend/app/presentation/api/__init__.py` | Mount the v1 router; register the handler; add `lifespan` (§10) |
| `backend/app/presentation/api/v1/__init__.py` | **Not in the draft.** Placeholder docstring becomes the `/api/v1` prefix router |
| `backend/app/presentation/api/v1/routers/__init__.py` | **Not in the draft.** Exports `images_router`, matching `routes/__init__.py`'s style |
| `backend/app/presentation/dependencies/__init__.py` | All three use-case/repository providers become `Depends`-based over `get_db` (§7) |
| `backend/app/infrastructure/config/settings.py` | `warm_up_models` (§10.2) |
| `backend/tests/presentation/test_dependencies.py` | Updated for the new provider signatures (§7) |
| `backend/tests/test_ai_layer_boundaries.py` | Walk `app/presentation/api/` (§11.4) |
| `.env.example` | `WARM_UP_MODELS` |

**Deleted**

| file | reason |
| --- | --- |
| `backend/app/presentation/api.py` | Shadowed by the `api/` package; would self-import if reachable (§6.2) |
| `backend/app/presentation/dependencies/dependencies.py` | Empty placeholder beside the real wiring (§6.2) |
| `backend/app/presentation/schemas/image_schema.py` | **Deleted, not filled.** The draft left the choice open; `search_schema.py` covers everything search returns, and an image schema is RFC-027's to write when it has a payload to describe. `collection_schema.py` is left alone — Collections is not on this RFC's path |

# 18. Validation

Filled in from a real run of 2026-08-23. Every row is a command, not a
claim.

| check | expected | result |
| --- | --- | --- |
| `pytest` | 471 + the new fast tests, 0 failures | **502 passed**, 58 deselected, 46.9 s. +31: 14 level-1, 5 level-2, 4 startup, 3 dependency, 5 boundary |
| `pytest` reaches no network | `test_health.py` still runs offline and fast (§10.1) | **38 passed in 18.6 s** with `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1` |
| `pytest -m slow` | 56 + the 2 new E2E tests | **58 passed**, 502 deselected, 168.6 s |
| `black --check .` | clean | **131 files unchanged** |
| `ruff check .` | clean | **All checks passed** |
| `mypy` | 5 pre-existing errors, **0 in new or modified code** | **5 errors in 4 files**, all pre-existing (2 abstract-instantiation tests, 1 unreachable, 1 arg-type, 1 generator return) |
| `alembic heads` | `26058b9e1d9a (head)`, unchanged | **`26058b9e1d9a (head)`** |
| Presentation router imports | no `sqlalchemy`, `torch`, `transformers`, `PIL`, `langdetect`, `clip` (§11.4) | Enforced by `test_ai_layer_boundaries.py`, now walking three directories |
| Application layer imports | unchanged from RFC-025 | Same walk, still green |
| Connections returned after N requests | pool checkout count back to baseline (§11.2) | `QueuePool.checkedout()` identical after 5 requests. Reintroducing the old provider fails **4 of the 5** level-2 tests and no others (§11.2) |
| The three deleted files | `grep` shows no importer before deletion (§6.2) | Verified before deleting. `from app.presentation.api import app` in `main.py` and `test_health.py` resolves to the *package*, which is what shadowed the shim |
| Indexing worker | CLI still runs; `get_embedding_model()` still zero-arg (§7.1) | `python -m app.infrastructure.workers.indexing_worker --root <tmp>` → **Discovered 1, Indexed 1, Failed 0** with the real checkpoint. The row it wrote was deleted afterwards |
| Development database | unchanged; every test rolled back | **3 rows before, 3 rows after**, same ids |
| `GET /api/v1/images/search?q=fish+ponds` by hand | 200, ranked JSON, against the real indexed corpus | **200 in 0.130 s**, `fish_ponds_02` first at `+0.3255`, `cleared_lot_04` last at `+0.2256` |

## 18.1 The endpoint by hand

Against a real `uvicorn main:app` with `WARM_UP_MODELS=true`, over the
development database. Startup logged `Embedding model warmed up in
11.23s` — higher than §10.2's 8.37 s, and the more honest of the two
numbers: the measurement script had already touched both checkpoints in
the same process, so its disk cache was hot in a way a freshly started
server's is not.

```
GET /health
{"status":"healthy","database":"connected","version":"0.1.0","environment":"development"}

GET /api/v1/images/search?q=fish+ponds                          200, 0.130 s
{"query":"fish ponds","limit":10,"results":[
  {"id":"8438ee26-...","filename":"fish_ponds_02","similarity":0.3255467622820445},
  {"id":"b082aa81-...","filename":"fish_ponds_01","similarity":0.3198034978147175},
  {"id":"603c940f-...","filename":"cleared_lot_01","similarity":0.2645992917300134},
  {"id":"80e09c3a-...","filename":"cleared_lot_04","similarity":0.22564180340518103}]}

GET /api/v1/images/search?q=uma%20propriedade%20rural%20com%20um%20lago&limit=3
                                                                200, 0.682 s
{"query":"uma propriedade rural com um lago","limit":3,"results":[
  {"id":"b082aa81-...","filename":"fish_ponds_01","similarity":0.2724458141592635},
  {"id":"8438ee26-...","filename":"fish_ponds_02","similarity":0.27095358198604524},
  {"id":"603c940f-...","filename":"cleared_lot_01","similarity":0.267724827988436}]}

GET /api/v1/images/search?q=                                    400
{"detail":"Search query cannot be empty or only whitespace."}

GET /api/v1/images/search?q=x&limit=101                         400
{"detail":"Search limit must be at most 100, got 101."}

GET /api/v1/images/search                                       422
{"detail":[{"type":"missing","loc":["query","q"],"msg":"Field required",...}]}
```

Four things to read off that, none of which a test asserts as neatly:

**The query echo is the raw input**, Portuguese and all, while the
ranking it produced is `fish_ponds_01`, `fish_ponds_02`, `cleared_lot_01`
— the translated neighbourhood. The prompt CLIP saw is nowhere in the
response (§5.2).

**No `path` anywhere**, on a database whose rows hold
`C:/Users/chapi/Documents/...` (§5.2).

**`limit` echoes 10 when it was never sent**, and 3 when it was (§5.2).

**400 and 422 arrive from different places and say different things**,
which is the boundary §8.1 exists to keep (§8.1).

The observability contract holds too — the log for the run above reads:

```
search requested: query_length=10, limit=10
search completed: results=4, elapsed_ms=119
search requested: query_length=0, limit=10
search requested: query_length=33, limit=3
search completed: results=3, elapsed_ms=674
```

No query text, no embedding, ever (§16.1). Note the refused request logs
a `requested` with no `completed`, which is exactly the shape a 400
should leave behind.

One caveat stated rather than buried: the corpus these requests ranked is
the four rows the development database happened to hold — three left by
an RFC-024 test plus the one the worker check indexed, since deleted.
Retrieval *quality* is measured over the 45-image corpus by
`tests/dataset/`, not here; this section is evidence that the transport
works, not that the model does.


# 19. What implementation changed about this draft

Kept as its own section rather than smoothed into the text above,
because a plan that reads as though it predicted everything is a plan
nobody can learn from. Six things moved, and none of them changed a
decision in §3 — which is the useful result: the shape was right, and
what the draft got wrong was the tree it was describing and the questions
it did not think to ask.

| # | the draft said | what shipped | why |
| --- | --- | --- | --- |
| 1 | `"filename": "fish_ponds_02.jpg"` | `"fish_ponds_02"` | `Image.filename` is `path.stem`; the extension is a separate field. The draft described a codebase that does not exist. The response publishes the field rather than rejoining the two, because a rejoined name is a value Presentation invented (§5.2) |
| 2 | warm-up "encodes one throwaway string" | the string is a Portuguese sentence | An English warm-up loads CLIP and leaves Marian's ~4.20 s in place for the first Portuguese query — half the cold start the draft quoted, in a product whose users write Portuguese (§10) |
| 3 | two providers grow `Depends` | all three did | `get_index_image_use_case()` composed itself out of `get_image_repository()` by direct call, so leaving it would have kept a second copy of the session leak alive (§7) |
| 4 | nothing about warm-up failure | logged at `WARNING`, startup continues | Aborting would take `/health` down with search and turn a Hugging Face blip into a crash loop (§10.2) |
| 5 | no startup test file | `test_api_startup.py` | §10.1 calls the unconditional warm-up a silent trap. A trap nothing tests is a trap (§17) |
| 6 | `image_schema.py` "filled, or removed" | removed | `search_schema.py` covers everything search returns; an image schema is RFC-027's to write when it has a payload (§17) |

And one prediction that held exactly, worth recording because it was the
risk this RFC was written around: **the route ended up four lines of
work** — resolve the default, call the use case, map the result — and
`tests/test_ai_layer_boundaries.py` now walks `app/presentation/api/` to
keep it that way. Nothing in the routing layer imports SQLAlchemy, torch,
transformers, PIL, langdetect, or anything named `clip`.
