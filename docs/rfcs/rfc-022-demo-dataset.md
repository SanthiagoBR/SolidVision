# RFC-022 — Demo Dataset

**Status:** Proposed
**Author:** SolidVision
**Depends on:** RFC-019 (Repository Implementations), RFC-020 (Incremental Metadata), RFC-021 (Indexing Worker)
**Blocks:** Real embedding model integration, vector search, benchmark RFC
**Last updated:** 2026-08-14

---

# 1. Context

The indexing pipeline is now complete end to end at the code level: `FilesystemImageProvider` discovers files, `IndexingWorker` drives `IndexOrUpdateImageUseCase`, and `PostgresImageRepository.save_indexed()` writes an image row together with its embedding into a `vector(1152)` column indexed by HNSW.

What does not exist is **any reproducible corpus to run it against**.

The only image data in the repository is `backend/test-images/`, containing three unrelated files (a cat, a car, one UUID-named photo) with no manifest, no attribution, and no recorded expectations. It cannot answer any of the questions the project now needs answered:

- Does the worker skip unchanged files, as RFC-020 specifies?
- Does a corrupt file get isolated without aborting the run?
- Are unsupported extensions actually filtered?
- Once a real embedding model exists, is retrieval quality good enough?
- Does the system meet the 100,000-image target in `ARCHITECTURE.md` §22?

This RFC defines the dataset that answers them.

---

# 2. Guiding Decision

The central design decision is that **"demo dataset" is not one artifact but three**, unified by a single manifest format.

Collapsing them into one folder of JPEGs produces a corpus that is simultaneously too small to benchmark, too large to commit, and too undocumented to assert against.

| Corpus | Size | Committed | Purpose |
|---|---|---|---|
| **Fixture** | ~15 files | No — generated at test time | Deterministic unit and integration tests |
| **Demo** | 40–60 real photos | Yes | Demos, E2E, manual QA, future semantic evaluation |
| **Scale** | 1k → 100k | No — generated, gitignored | Benchmarks against the §22 performance targets |

The lasting asset is the **manifest**, not the images. Images are replaceable; the manifest encodes expectations, provenance, licensing and — once a real model exists — retrieval ground truth. Tooling is written once against the manifest format and serves all three corpora.

---

# 3. Goals

- Provide a reproducible corpus exercising the full pipeline: filesystem → worker → use case → PostgreSQL → pgvector.
- Make RFC-020's incremental-skip behavior testable, which it currently is not (see §7.2).
- Cover structural edge cases that the worker's per-file error isolation is supposed to handle.
- Ship semantic-retrieval ground truth **now**, dormant, so that the day a real embedding model lands it becomes an evaluation set with zero rework.
- Record licensing and attribution for every committed image.
- Introduce no new Domain concept, no new port, and no new abstraction inside `app/`.

## Non-goals

- Integrating a real embedding model. Out of scope; this RFC prepares for it.
- Implementing vector similarity search. `SearchImagesUseCase` remains a stub.
- Running the 100k benchmark. Blocked on §7.4; deferred to a benchmark RFC.
- Anything touching `Collection` beyond reserving a manifest field.

---

# 4. Layout

```text
backend/
├── dataset/                        # data only — no Python, not a package
│   └── demo/
│       ├── manifest.json           # built
│       ├── queries.json
│       └── images/
│           ├── aerial/rural/       # built — 21 photos
│           ├── aerial/urban/       # built — 19 photos
│           └── everyday/           # built — 5 photos, Commons CC0/CC-BY (§9.5)
│
├── dataset_tools/                  # Python — manifest parsing, materialization
│   ├── __init__.py                 # built
│   ├── reencode.py                 # built
│   ├── manifest.py
│   ├── materialize.py
│   ├── seed_demo.py                # python -m dataset_tools.seed_demo
│   └── generators/
│       ├── __init__.py             # built
│       ├── hard_cases.py           # built — generated, never committed
│       ├── fixture_corpus.py
│       └── scale_corpus.py
│
└── tests/dataset/
    ├── test_hard_cases.py          # built
    ├── test_manifest.py
    └── test_demo_corpus_indexing.py

data/                               # gitignored runtime scratch (indexing_root_path)
```

## 4.1 Why `dataset_tools/` and not `datasets/`

`pyproject.toml` sets `pythonpath = [".", "backend"]`, so any top-level directory under `backend/` becomes importable by its bare name. A package named `datasets` would shadow HuggingFace's `datasets` library — which is likely to be installed alongside `transformers` when the SigLIP adapter arrives. The resulting import failure would be confusing and would surface far from its cause.

`dataset_tools` has no such collision. The data directory `dataset/` deliberately contains no `__init__.py` and no `.py` files, so it is never importable at all.

## 4.2 Why this code does not live in `app/`

`dataset_tools/` is development tooling, not shipped application behavior. Placing it under `app/infrastructure/` would grow the production package with code that only tests and demos invoke, and would invite a `DatasetPort` abstraction that the architecture does not need.

The seeding path deliberately reuses the existing production components rather than reimplementing them:

```text
manifest.json
      │
      ▼
materialize()  ──►  temp/demo root on disk (mtimes stamped)
                          │
                          ▼
                 FilesystemImageProvider
                          │
                          ▼
                  IndexingWorker  ──►  IndexOrUpdateImageUseCase
                          │
                          ▼
                 PostgresImageRepository
```

Seeding is therefore itself an end-to-end test of RFC-021, not a parallel implementation of it.

---

# 5. Manifest Format

## 5.1 Format choice: JSON, not YAML

`backend/requirements.txt` does not include PyYAML, and the project has consistently avoided dependencies it does not need. JSON is parseable with the standard library and types cleanly under `mypy --strict`.

**Tradeoff:** JSON has no comments, so per-entry rationale must go in a dedicated field rather than an inline comment. Accepted.

## 5.2 `manifest.json`

```json
{
  "version": 1,
  "license_note": "All images are CC0 or CC-BY. Per-entry attribution below.",
  "images": [
    {
      "relative_path": "images/aerial/rural/lake_property_01.jpg",
      "collection": "demo-aerial",
      "file_modified_at": "2024-06-01T12:00:00Z",
      "caption": "Rural property with a house, outbuildings, and a small lake, with a horse grazing nearby",
      "tags": ["aerial", "rural", "property", "house", "water", "lake", "horse"],
      "source_url": null,
      "license": "proprietary-permitted",
      "attribution": "the project's photographer",
      "expect": "indexed",
      "burst_group": "lake_property"
    }
  ]
}
```

Field contracts:

| Field | Contract |
|---|---|
| `relative_path` | Identity key. Relative to the corpus root, posix separators. **Never a UUID** — see §7.1. |
| `collection` | Reserved. No consumer today; `Collection` does not exist yet. Prevents rebuilding the manifest when it does. |
| `file_modified_at` | Authoritative mtime, stamped onto the file at materialization. See §7.2. |
| `caption` / `tags` | Human-readable semantics. Input to `queries.json` authoring and future evaluation. |
| `source_url` | URL of the original, or `null` for the photographer's own unpublished work. See §8. |
| `license` | `proprietary-permitted` for the photographer's originals; `CC0` / `CC-BY-4.0` etc. for Commons-sourced negatives. See §8. |
| `attribution` | Mandatory for every committed image, but need not be a full legal name — see §8. |
| `expect` | `indexed` \| `ignored` \| `failed`. The assertion the test suite enforces on a first run — vocabulary defined in §6.2. |
| `burst_group` | Optional. Groups frames from the same drone orbit/pass together — several were shot as near-duplicate bursts of a single site. Feeds the near-duplicate case in §6.1 directly; entries without a sibling omit this field. |

## 5.3 `queries.json`

```json
{
  "version": 1,
  "queries": [
    {
      "query": "rural property with a lake",
      "relevant": [
        "images/aerial/rural/lake_property_01.jpg",
        "images/aerial/rural/lake_property_02.jpg"
      ],
      "hard_negatives": [
        "images/aerial/rural/river_farmland_03.jpg"
      ]
    }
  ]
}
```

This file ships **dormant**. Its consuming test is marked `xfail(strict=False)` with a reason naming the blocking dependency, because `FakeEmbeddingModel` cannot satisfy it (§7.3). When a real model lands, removing the marker activates the evaluation set with no change to the data.

---

# 6. Content Plan

## 6.1 Demo corpus

Chosen so that retrieval quality is actually *demonstrable* rather than trivially satisfied:

- **Target domain, densely sampled** — aerial rural and property photography, matching the stated use case in `ARCHITECTURE.md` §1. This is the bulk of the corpus.
- **Hard negatives** — aerial forest vs. aerial farmland; river vs. lake. A system that only separates cats from farmland proves nothing. Ranking quality is visible only in near-misses.
- **Near-duplicates** — the same scene from two angles. Feeds the duplicate-detection roadmap item (§23).
- **Trivial negatives** — a small number of everyday photos, absorbing the current `test-images/` content.

**As built**, the 40 delivered photos came in as 8 drone bursts and split 21 rural / 19 urban, rather than the rural-dominant skew assumed above. Two consequences:

- The near-duplicate requirement is satisfied generously — most frames are orbit siblings of one site, tracked by the manifest's `burst_group` field.
- The hard-negative requirement is satisfied by content that emerged rather than content that was sought: a swimming pool, artificial fish-farming ponds, and a natural lake all appear as distinct water bodies across `hillside_property_pool_*`, `fish_ponds_*`, and `lake_property_*`. Distinguishing those three is a genuinely hard retrieval task and a better test than the forest-vs-farmland pair originally proposed.
- The **trivial negatives bucket** is populated separately from `test-images/`, which was deleted outright rather than folded in. `images/everyday/` now holds 5 CC0/CC-BY photos (cat, dog, bicycle, coffee cup, laptop) pulled from Wikimedia Commons via its API — see §9.5.

## 6.2 Hard cases

Where the dataset earns its value as a whole-system test.

**These are generated, not committed** (resolving open question 1 in §13). Every case can be synthesized faithfully with Pillow, which avoids committing deliberately-corrupt binaries and avoids depending on how a given filesystem or git client normalizes an accented filename. They are produced by `dataset_tools/generators/hard_cases.py` into a temporary root at test time, and the corpus is byte-for-byte deterministic across runs.

Expectations live in the generator module beside the code that writes the files, not in `manifest.json`, so a case and its asserted behavior cannot drift apart. A test asserts the generated tree and the declared table describe exactly the same set.

### Outcome vocabulary

`expect` describes a **first** run against an empty database:

| Value | Meaning |
|---|---|
| `indexed` | Discovered, and a row is written |
| `ignored` | Never discovered — filtered by the extension check in `FilesystemImageProvider.discover()`, so it never reaches the use case |
| `failed` | Discovered, but indexing raised and was caught by the per-file `except` in `IndexingWorker.run()` |

This replaces the earlier `indexed | skipped | failed` triple. "Skipped" was ambiguous between *filtered at discovery* and *unchanged on a later run* — two unrelated things. The incremental-skip outcome is not an `expect` value at all: it is a property of a second run, uniform across everything that indexed on the first, and is asserted once by the two-run test.

### Cases

| Case | `expect` | Notes |
|---|---|---|
| CMYK JPEG, grayscale JPEG, 16-bit TIFF, RGBA PNG | `indexed` | |
| EXIF-rotated JPEG (orientation 6) | `indexed` | Orientation handling deferred to the thumbnail RFC |
| `.JPG` uppercase extension | `indexed` | `extension` persists lowercased |
| `fazenda São João.jpg` | `indexed` | Spaces and non-ASCII round-trip through `ImagePath` |
| Deeply nested subdirectory | `indexed` | Reached only via `rglob` |
| Identical content at two paths | `indexed` ×2 | Produces **two** rows — pins the path-derived id in §7.1 |
| Zero-byte file | `indexed` † | |
| Truncated JPEG | `indexed` † | |
| `.gif`, `.txt` | `ignored` | Filtered at discovery; never reach the use case |

† **These index cleanly today, and that is the finding, not an oversight.** `FakeEmbeddingModel.encode_image()` hashes the id and path only — it never opens the file (§7.3). Nothing in the current pipeline decodes pixels, so a file whose *bytes* are broken still produces a perfectly good row. The generator records the future outcome in a separate `expect_with_pixel_decoding` field (`failed` for both), which activates the day a real embedding model, thumbnail generator, or metadata extractor first opens an image. Asserting `failed` today would fail against real behavior; asserting `indexed` without recording the intent would lose it.

The duplicate-path row is not a bug being enshrined; it is current, deliberate behavior documented in `image_identity.py`, and pinning it means any future change to the id scheme fails loudly rather than silently.

---

# 7. Constraints in the Existing Code

Four properties of the current implementation directly constrain this design. Each is a real behavior of committed code, not a hypothetical.

## 7.1 `ImageId` depends on the absolute path

`compute_image_id()` derives the id as `uuid5(SOLIDVISION_PATH_NAMESPACE, str(path))`, and `IndexingWorker` feeds it `discovered.path` straight from `rglob`, which is absolute whenever the scan root is.

The same image therefore receives a **different UUID on every machine and every checkout location**.

**Consequence:** the manifest keys on `relative_path` and never on a UUID, and the loader computes ids at load time from the resolved root. No golden file may contain a hardcoded id.

This also makes a latent design tension concrete: ids are not portable across machines, which will matter for any future collection-export or multi-machine feature. Surfacing that pressure is a secondary benefit of this RFC; changing the scheme is explicitly out of scope and would require its own ADR and a migration strategy, as `image_identity.py` already warns.

## 7.2 Git does not preserve mtimes

A fresh clone stamps every committed image with checkout time. Any incremental-indexing test over the demo corpus would therefore be non-reproducible — the first run indexes, and whether the second run skips depends on filesystem timing rather than on the logic under test.

**Consequence:** `file_modified_at` is mandatory in the manifest, and `materialize()` applies it with `os.utime()` after copying. This is precisely what makes RFC-020's skip-unchanged path testable at all, and it is the single most important detail in this RFC.

## 7.3 `FakeEmbeddingModel` never reads pixels

`encode_image()` hashes `f"image::{image.id}::{image.path}"`. The image content is never opened.

**Consequence:** today this dataset can validate *pipeline* behavior only — discovery, filtering, change detection, skip/update, persistence, vector writes. It cannot validate retrieval quality. `queries.json` ships dormant rather than being deferred to a later RFC, because authoring ground truth is manual work best done while assembling the images, and doing it later would mean handling every image twice.

## 7.4 Fake vectors were near-degenerate under cosine distance (fixed)

In the original `_build_embedding`, the positional term `(index + 1) * 0.125` reached **144** at the final dimension, while the content-derived term stayed within `(0, 1]`. Every vector produced was the same steep ramp plus a negligible perturbation, so cosine similarity between any two images was ≈ 1.0.

**Consequence:** HNSW **recall** measurements over a scale corpus would have been meaningless. Index build time and query latency would still be real, but any recall figure would be an artifact of degenerate input. This is why the 100k benchmark stays deferred, independent of this fix.

**Fixed.** `_build_embedding` now expands the seed via SHA-256 in counter mode into `embedding_dimension` floats spread across `[-1, 1)`, mean-centers them, and L2-normalizes the result to a unit vector. SHA-256 was chosen over Python's built-in `hash()` specifically because `hash()` is salted per-process via `PYTHONHASHSEED` and is not reproducible across runs or machines — determinism is a hard requirement here (`ARCHITECTURE.md` section 20), and normalization does not weaken it: the same seed hashes to the same bytes every time, on every machine.

Empirically, cosine similarity between unrelated seeds (`"cat"`, `"dog"`, `"rural property with a lake"`, `"downtown office tower"`) now spreads across roughly ±0.07, centered on 0 — matching the theoretical behavior of random unit vectors in a 1152-dimension space (expected standard deviation ≈ 1/√1152 ≈ 0.029). An identical seed still yields cosine similarity 1.0 with itself. New tests in `test_fake_embedding_model.py` pin unit-norm, mean-centering, and the cross-seed spread directly, so a regression to the old ramp behavior fails loudly.

**Tradeoff, realized:** this changed every vector the fake model produces. No existing test asserted a literal fake-embedding value, so nothing needed updating beyond the new tests added alongside the fix.

---

# 8. Licensing

`backend/test-images/` currently holds Wikimedia-sourced photographs with no recorded attribution, one of them 2 MB. The repository carries a `LICENSE` file and git history is permanent, which makes this worth resolving now rather than later.

The 40-image aerial demo corpus consists entirely of original work by the project's photographer — the same aerial photographer described in `ARCHITECTURE.md` §1 — included in this public repository with their explicit, confirmed permission. These are not Creative Commons images and carry no `source_url`; `license` is recorded as `proprietary-permitted` per manifest entry, and `attribution` is intentionally non-identifying (`"the project's photographer"`) rather than a full name, at the photographer's preference.

Because these are real, identifiable properties, two additional precautions apply beyond what a CC-sourced corpus would need:

- `dataset_tools/reencode.py` bakes in EXIF orientation and then strips all EXIF, including GPS coordinates, before an image is committed. No image reaches `dataset/` with location metadata intact.
- Consent covers the specific 40 images placed in `dataset/demo/`. It does not extend automatically to future additions from the same photographer — each new batch needs its own confirmation before being committed, re-encoded or not.

Everyday negatives (§6.1) still draw from Wikimedia Commons or similarly-licensed sources, since they carry no such consent requirement. For that content:

- Require `source_url`, `license`, and `attribution` on every entry.
- Restrict to CC0 or CC-BY.

This RFC also:

- Re-encodes every committed image to roughly 1024 px on the long edge, targeting under 200 KB each, keeping total committed weight in the low single-digit megabytes.
- Folds the salvageable `test-images/` files into the demo corpus with proper attribution and removes the directory.

---

# 9. Deliverables

| # | Deliverable | Status |
|---|---|---|
| 1 | `backend/dataset/demo/` — licensed images, `manifest.json`, `queries.json` | **Done** — 45 images (40 aerial + 5 everyday), `manifest.json`, `queries.json` |
| 2 | `dataset_tools/manifest.py` — typed manifest model and loader; raises on schema violations | **Done** |
| 3 | `dataset_tools/materialize.py` — copies a corpus into a target root and stamps mtimes | **Done** |
| 4 | `dataset_tools/generators/fixture_corpus.py` — Pillow-generated deterministic fixtures | **Not built — superseded, see 9.4** |
| 5 | `dataset_tools/generators/scale_corpus.py` — generates *N* synthetic images into a gitignored root | **Done** |
| 6 | `dataset_tools/seed_demo.py` — CLI wiring the real components into PostgreSQL | **Done** — `python -m dataset_tools.seed_demo`; see 9.3 for a default-target correction made while building it |
| 7 | `demo_corpus` pytest fixture materializing into `tmp_path` with correct mtimes | **Done** — `backend/tests/dataset/conftest.py` |
| 8 | Tests under `backend/tests/dataset/` asserting every `expect` value, plus a two-run test | **Done** — 66 tests: manifest validation, materialize correctness, hard cases, queries.json structural validity plus a dormant retrieval test, scale-corpus generation, and the full Postgres-backed pipeline including a two-run skip test and a touch-one-file test |
| 9 | `FakeEmbeddingModel` normalization fix (§7.4) | **Done** |
| 10 | Removal of `backend/test-images/` | **Done** |
| 11 | `dataset_tools/reencode.py` — re-encode to the §8 spec, strip EXIF/GPS | **Done** (added; not in the original list) |
| 12 | `dataset_tools/generators/hard_cases.py` — generated edge-case corpus | **Done** |

**Remaining gap:** `images/everyday/` (§6.1's trivial-negatives bucket) is still empty. This is a content-sourcing task (CC-licensed everyday photos from Wikimedia Commons, per §8), not a code task, and nothing built so far depends on it.

## 9.1 Configuration touchpoints

- `pyproject.toml` — register any new pytest marker under `[tool.pytest.ini_options] markers`. `addopts` includes `--strict-markers`, so an unregistered marker is a hard error, not a warning. *(No marker needed yet; the hard-case tests require no database.)*
- `pyproject.toml` — **done:** `backend/dataset_tools` added to `[tool.mypy] files`.
- `pyproject.toml` — **done:** `dataset_tools` added to `[tool.ruff.lint.isort] known-first-party`, which previously listed only `app` and so sorted the new package as third-party.
- `.gitignore` — **done:** added `data/`. Covers both `seed_demo.py`'s materialized output (`data/demo/`) and `scale_corpus.py`'s default output (`data/scale/`) — both default under the already-ignored `data/`, so no separate entry was needed for the scale corpus after all.
- No `Settings` change is required; `indexing_root_path` already exists and is the injection point for the corpus root.

## 9.2 Pre-existing blocker: mypy could not run repo-wide (fixed)

`python -m mypy` used to fail before checking anything:

```text
backend\tests\application\fakes.py: error: Source file found twice under
different module names: "application.fakes" and "tests.application.fakes"
Found 1 error in 1 file (errors prevented further checking)
```

Root cause: `backend/app/__init__.py` exists but `backend/__init__.py` does not, so mypy discovered `backend` as an implicit search root while walking the `app/...` files in `files =`. That same root let `tests` resolve as a namespace package for the `from tests.application.fakes import ...` statements used by four test files. But `backend/tests/application/fakes.py` was *also* walked directly as part of `files = [..., "backend/tests", ...]`, where mypy's climb-through-`__init__.py` algorithm stopped one level early — at `backend/tests`, since `tests/` itself had no `__init__.py` — producing a different module name for the same file. Two names for one file is a hard stop for mypy.

This predates RFC-022 (reproduces with the `[tool.mypy]` files= change reverted) and meant strict type checking had been silently non-functional repo-wide.

**Fixed** by adding `backend/tests/__init__.py`, matching mypy's own suggested resolution (a) in the error message. Both module-name computations now agree on `tests.application.fakes`. Verified: `mypy` checks 93 files repo-wide without crashing; `pytest` still passes (167/167); `ruff`/`black` clean (ruff auto-reordered three now-differently-classified imports, cosmetic only).

Doing so retired the crash but did not fix anything else — it surfaced 5 pre-existing type errors in existing test files that mypy had never previously reached: an abstract-class instantiation in `test_embedding_model_port.py`, an unreachable `isinstance` check and a `list[float] | None` argument-type error in `test_postgres_image_repository.py`, and a fixture return-type mismatch in `test_health.py`. None are new; none are addressed by this RFC.

## 9.3 `seed_demo.py`'s target must not default to `indexing_root_path`

The first implementation defaulted `--target` to `settings.indexing_root_path` — the directory a production worker CLI will eventually scan for a user's real photo collection. That default was live-tested against the local dev database before this section was written, which is what surfaced the problem: `indexing_root_path` already ends in `images`, and every manifest `relative_path` starts with `images/`, so the materialized output landed at a doubled `data/images/images/aerial/...`. Cosmetic on its own, but it exposed the real issue underneath — the moment a production worker CLI exists, defaulting the demo seeder to the same directory means running `seed_demo.py` from habit would silently mix 40 demo photos into a user's real, configured collection.

The default is now `data/demo` (independent of `indexing_root_path`, not derived from it, so an unusual `INDEXING_ROOT_PATH` override can't produce a strange derived path either). Materialized output now mirrors the source corpus layout exactly: `data/demo/images/aerial/...`. Serving the demo data through the running app requires pointing `INDEXING_ROOT_PATH` at `data/demo` explicitly — a deliberate, visible `.env` change, not a script default.

Verified end to end against the local dev PostgreSQL container: a first run indexed all 40 manifest entries, and an immediate second run skipped all 40, confirming the incremental-skip path (RFC-022 section 7, §10) holds in a real invocation and not only under the test suite's SAVEPOINT isolation.

## 9.4 `fixture_corpus.py` was not built

The original design (§2) called for a Pillow-generated "fixtures" corpus, distinct from `hard_cases.py`'s structural edge cases, for general-purpose deterministic unit/integration testing. Before building it, a check of the existing test suite (`test_filesystem_image_provider.py`, `test_indexing_worker.py`) showed the established, working pattern for pipeline/orchestration tests is `path.write_bytes(b"data")` — raw placeholder bytes, no Pillow at all. This is correct, not a shortcut: `FilesystemImageProvider` only reads filesystem metadata (extension, size, mtime), and `FakeEmbeddingModel` never opens a file's pixels (§7.3), so nothing in these tests benefits from real image content.

That leaves no concrete consumer for a generic "N valid synthetic images" corpus. Anything needing real, openable image structure is already served by `hard_cases.py` (CMYK, EXIF, 16-bit, alpha, etc.); anything needing only a supported extension is already served by the one-line raw-bytes pattern above. Building `fixture_corpus.py` anyway would have been premature abstraction — a third mechanism duplicating what the other two already cover, with no test in the suite that would import it. Deliverable 4 is marked not built rather than done; it can be revisited if a genuine consumer appears (e.g. a future thumbnail generator that needs valid-but-arbitrary image content at volume).

## 9.5 Sourcing `images/everyday/` from Wikimedia Commons

Five trivial negatives — cat, dog, bicycle, coffee cup, laptop — were pulled from Commons via its `action=query` API rather than by hand, using `iiprop=extmetadata` to get machine-readable `LicenseShortName`, `Artist`/`Credit`, and `AttributionRequired` fields per candidate, exactly as planned when this RFC was first drafted. Process, not automated into a committed script:

1. Query `generator=categorymembers` against a handful of everyday categories (`Category:Domestic cats`, `Category:Dogs`, `Category:Bicycles`, `Category:Coffee cups`, `Category:Laptops`), filtering results to `image/jpeg` and `LicenseShortName` in `{CC0, CC BY *, Public domain}` — never CC-BY-SA, per §8's CC0-or-CC-BY restriction for non-photographer content.
2. Fetch full `extmetadata` (including `LicenseUrl`) for the specific chosen titles, and download from the API's own `imageinfo.url` field verbatim rather than a hand-reconstructed URL — a first attempt that retyped three URLs by hand hit a silent failure (see below).
3. Open every downloaded file and visually confirm it matches its title before it goes anywhere near the repository. Not a formality: this is the same content-verification discipline applied to the photographer's 40 aerial photos, extended to third-party sourcing.
4. Re-encode through the existing `dataset_tools/reencode.py` — no separate code path for Commons-sourced images.
5. Add manifest entries with real `source_url`, `license`, and `attribution` values taken directly from the fetched `extmetadata`, tagged `trivial-negative`.

**Two failures worth recording**, both caught by verification rather than assumed away:

- Retyping three of the five download URLs by hand (rather than using the API's own `url` field) produced three files that were actually generic Wikimedia HTML — not JPEGs — despite matching expected byte-for-byte identical sizes across all three (a red flag in itself: three *different* subjects should not download to the *same* size). Inspecting the content showed HTTP 429 "Too many requests," not a URL-encoding bug — five requests fired back-to-back exceeded a rate limit. Fixed by re-fetching with delays between requests, and by preferring the API-provided URL string going forward rather than reconstructing it.
- `manifest.json`'s `license_note` field, until this section, still asserted "no third-party or stock imagery is included here" — true when written, false the moment Commons content was added. Updated to describe both license models the manifest now carries side by side.

Manifest total: 45 entries (40 aerial + 5 everyday), confirmed against the directory with `Manifest.verify_matches_directory()`, and confirmed indexing correctly end to end via `seed_demo.py` against the local PostgreSQL container (45 rows, matching exactly).

One test assumption needed correcting once these landed: `test_every_manifest_entry_is_relevant_for_at_least_one_query` (§10) had asserted every manifest entry appears in some query's `relevant` list. Trivial negatives are supposed to be irrelevant to *every* query by design — forcing one into a `relevant` list to satisfy the test would contradict its purpose. Narrowed to exclude entries tagged `trivial-negative`, with the reasoning recorded in the test itself.

---

# 10. Testing Strategy

| Level | Corpus | Requires PostgreSQL | Status |
|---|---|---|---|
| Manifest schema validation | — | No | Done — 24 tests |
| Discovery, filtering, hard cases | Generated (`hard_cases.py`) | No | Done — 20 tests |
| Materialize correctness (copy + mtime stamping) | — | No | Done — 6 tests |
| Full indexing + persistence | Demo (materialized) | Yes | Done — 4 tests |
| Incremental skip on second run | Demo (materialized) | Yes | Done |
| Scale-corpus generation (structure only, small counts) | Generated (`scale_corpus.py`) | No | Done — 5 tests; the 100k benchmark run itself stays out of scope (§14) |
| `queries.json` structural validity | Demo + `queries.json` | No | Done — 6 tests |
| Retrieval quality | Demo + `queries.json` | No | Built, **dormant — xfail** until a real `EmbeddingModelPort` exists |

Database-backed tests use the existing `db_session` fixture from `conftest.py`, which isolates each test in a SAVEPOINT rolled back on teardown. No test may write to the development database outside that transaction.

Unit-level tests must not require a materialized demo corpus; that is what `hard_cases.py`'s generated corpus is for (§9.4 explains why a separate `fixture_corpus.py` was not also built).

---

# 11. Alternatives Considered

**One flat folder of committed JPEGs.** Rejected. Cannot reach benchmark scale without bloating git history, and carries no expectations, so tests must hardcode filenames and drift silently as the folder changes.

**Generate everything, commit nothing.** Rejected. Synthetic images cannot support semantic evaluation, and the demo has no value as a demo if the images are noise squares. The split in §2 keeps generation where generation works.

**Download images at test time from a remote URL.** Rejected. Introduces network dependency and non-determinism into the test suite, and violates the project's local-first premise.

**YAML manifests.** Rejected — see §5.1.

**Defer `queries.json` until a real model exists.** Rejected — see §7.3.

---

# 12. Risks

| Risk | Mitigation |
|---|---|
| Manifest drifts from the files on disk | A test asserts the manifest and the directory tree describe exactly the same set, in both directions |
| Committed images inflate repository size | Hard cap: ~1024 px long edge, <200 KB per image, enforced by a test |
| `hard_cases/` fixtures are platform-dependent (accented filenames on Windows) | Generated rather than committed; verified working on the Windows dev environment this RFC was implemented on |
| §7.4 normalization breaks existing tests | Expected and accounted for in §9; only tests asserting literal vector values are affected |

---

# 13. Open Questions

1. ~~Should `hard_cases/` be committed or generated?~~ **Resolved: generated.** Every case proved synthesizable with Pillow, including the CMYK and EXIF-orientation cases that motivated the doubt, so nothing needed committing. Output is byte-for-byte deterministic across runs. See §6.2.
2. ~~Should the scale-corpus generator produce real JPEGs or empty files with plausible metadata?~~ **Resolved: minimal real JPEGs.** `scale_corpus.py` generates tiny (8×8 px) but genuinely valid, Pillow-openable JPEGs, sharded across subdirectories to avoid the filesystem degradation a single 100k-entry directory would cause on NTFS. Empty files would have been faster to produce, and remain sufficient for today's `FakeEmbeddingModel`, which never opens pixels — but the zero-byte hard case (§6.2) already demonstrates that choice's failure mode concretely: it indexes cleanly today and is expected to fail once a real model decodes pixels. Using minimal-but-valid files instead means the scale corpus does not need regenerating the day a real embedding model lands.

Neither question blocks the rest of the RFC; both are now closed.

---

# 14. Out of Scope

- Real embedding model integration.
- Vector similarity search and a functional `/search` endpoint.
- The 100,000-image benchmark run itself. §7.4's normalization fix removed the reason it would have been *meaningless*, and `scale_corpus.py` (§9) provides the generator, but running and reporting the measured benchmark is separate work — `ARCHITECTURE.md` §22 reserves `scripts/benchmark.py` for it.
- Any change to `ImageId` derivation (§7.1).
- `Collection` entity, table, or persistence.
