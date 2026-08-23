# RFC-026 Search API -- end-to-end check for a friend

One script, `friend_e2e_check.py`, that answers a single question:

> On this machine, right now, does a real HTTP request reach real CLIP
> and real PostgreSQL and come back as the JSON RFC-026 promises?

No pytest, no markers to remember, no reading the RFC first. Clone,
follow the three setup steps below, run one command, read PASS or FAIL.

## 1. Get the code

```bash
git checkout bakeoff/rfc-026-search-api
cd SolidVision
python -m venv backend/.venv
```

Activate it (`backend\.venv\Scripts\Activate.ps1` on Windows,
`source backend/.venv/bin/activate` on Bash), then:

```bash
pip install -r backend/requirements.txt
```

## 2. Start PostgreSQL and apply migrations

```bash
docker compose up -d
cd backend
alembic upgrade head
```

Confirm `alembic heads` prints exactly `26058b9e1d9a (head)` -- RFC-026
adds no migration, so this number should look identical to whatever you
last saw on `develop`.

You need a `.env` at the repo root with at least:

```
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_NAME=solidvision
DATABASE_USER=solidvision
DATABASE_PASSWORD=solidvision
```

(`.env.example` has the full list with comments; copy it to `.env` and
adjust the database block above if `docker compose up -d` used
different values.)

## 3. Run the check

From the repo root, with the virtualenv active:

```bash
python experiments/rfc-026-search-api/friend_e2e_check.py
```

It will:

1. Index the 45-image RFC-022 demo corpus with the real CLIP model,
   inside a database transaction that is **rolled back at the end** --
   nothing it does is kept, your real data is untouched.
2. Boot the actual FastAPI app under a real `uvicorn` server on
   `127.0.0.1:8127`.
3. Fire real HTTP requests at `/health` and `/api/v1/images/search` --
   an English query, a Portuguese query (checking translation actually
   ran), and the three documented error cases (blank query, an
   out-of-range `limit`, a missing `q`).
4. Print one `[PASS]`/`[FAIL]` line per check, then a summary, and exit
   with status `1` if anything failed.

**First run downloads two Hugging Face checkpoints** -- the CLIP model
(~5 s to load once cached) and, the first time a Portuguese query runs,
the Marian translator (~5 s more). That is expected on a cold run, not a
hang; every run after the first is fast because both are cached to disk.

### What PASS looks like

```
======================================================================
RFC-026 Search API -- end-to-end check
======================================================================

1. Checking the database connection...
  [PASS] PostgreSQL is reachable

2. Indexing the demo corpus (rolled back afterwards, nothing kept)...
   Loading CLIP (first run downloads it -- can take a minute)...
  [PASS] Indexed the 45-image demo corpus (45 indexed, 0 failed)

3. Starting the real API server on a real socket...
  [PASS] The server started

4. Checking /health...
  [PASS] GET /health -> 200
  [PASS] GET /health reports the database connected

5. Checking a real English search...
  [PASS] English search -> 200
  [PASS] response has query, limit, and results
  [PASS] a relevant image (a fish pond) is in the top 5
  [PASS] no result exposes a server filesystem path
  [PASS] every similarity is a float in [-1, 1]

6. Checking a Portuguese search is translated...
  [PASS] Portuguese search -> 200
  [PASS] Portuguese and its English equivalent overlap in the top 5 (proves translation ran, not just CLIP)
  [PASS] the response echoes the Portuguese query verbatim, not the English prompt CLIP saw

7. Checking the documented error cases...
  [PASS] blank query -> 400
  [PASS] limit=101 -> 400
  [PASS] missing q -> 422

======================================================================
ALL 16 CHECKS PASSED
======================================================================
```

Exit code `0` on that outcome, `1` otherwise -- wire it into anything
that wants a yes/no answer.

## Troubleshooting

| symptom | likely cause |
| --- | --- |
| `Could not reach PostgreSQL` | `docker compose up -d` isn't running, or `.env` doesn't match it -- try `docker compose ps` |
| Hangs for a long time on "Loading CLIP" | First-run download; needs internet access to huggingface.co. Watch for a progress bar -- if there truly is none after a minute or two, check your network |
| `Indexed the 45-image demo corpus` fails with fewer than 45 | Check `backend/dataset/demo/` is present and untouched -- `manifest.verify_matches_directory()` fails loudly if a file moved or changed |
| `The server started` fails | Port 8127 already in use by something else on this machine |
| Everything fails at once with a traceback | Read the traceback -- it's printed in full, this isn't swallowed |

## What this is not

It is not a retrieval-quality benchmark. `Recall@5`, hard-negative
contamination, and the rest of RFC-025's measured floors are
`backend/tests/dataset/`'s job, run with `pytest -m slow` against the
same 45-image corpus. This script only proves the *transport* -- that a
real request reaches the real model and the real database and comes
back shaped the way `docs/rfcs/rfc-026-search-api.md` §5 says it will.
If you want the full detail behind every decision this checks, that RFC
is the place to read it.
