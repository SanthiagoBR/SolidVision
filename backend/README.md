# SolidVision Backend

This directory contains the initial backend structure for SolidVision.

It follows the Clean Architecture layout described in the project documentation and contains only placeholder modules and package scaffolding.

## Python version
This project targets Python 3.12.

## Create a virtual environment
```bash
python -m venv .venv
```

## Activate the environment
On Windows PowerShell:
```powershell
.\.venv\Scripts\Activate.ps1
```

On Bash:
```bash
source .venv/bin/activate
```

## Install dependencies
```bash
pip install -r backend/requirements.txt
```

## Run Ruff
```bash
ruff check backend
```

## Run Black
```bash
black backend
```

## Run MyPy
```bash
mypy backend
```

## Run Pytest
```bash
pytest backend/tests
```

## Configuration and user directories (RFC-034)

### Two modes, read from the disk
The backend is in `development` mode when `pyproject.toml` is at the root of
the tree the code is running from, and in `installed` mode otherwise. Nothing
sets the mode: no variable, no file. `GET /health` reports it, and every
process writes it in the first lines of its log, with the file it read and the
directories it resolved.

The tree is the same in both modes:
```text
<root>\
    backend\           app\, alembic\, alembic.ini
    runtime\postgres\  the embedded PostgreSQL runtime
    runtime\models\    CLIP and the translator, from the lockfile (RFC-035)
    pyproject.toml     only in a checkout
    .env               only in a checkout
```

### Where the settings come from
Strongest first:

| | development | installed |
|---|---|---|
| 1 | constructor arguments (tests) | constructor arguments (tests) |
| 2 | the process environment | — |
| 3 | `<repository>\.env` | `%LOCALAPPDATA%\SolidVision\solidvision.env` |
| 4 | defaults | defaults |

Each mode reads one file and never the other's. Installed, the process
environment is not a source: a `DEBUG=*` left there by another program changes
nothing. The connection to the embedded cluster is in neither column: the
engine reads the port and the password from the cluster at every new
connection. Nor do libpq's own variables reach it: before connecting to the
cluster, a process removes every `PG*` variable from its environment, because
`PGTZ`, `PGSERVICE` and a dozen others would change or refuse the connection.
With the `external` source they stay yours.

Both files are dotenv, UTF-8 with or without a BOM, with the keys of
`.env.example`. `solidvision.env` is optional and the app never creates it.
In Notepad, set **Save as type** to *All files* before saving it: with the
default type, Notepad saves `solidvision.env.txt`, which the app does not read
(it says so in the log).
- A key that is not a setting, a retired key (`ENVIRONMENT`,
  `INDEXING_ROOT_PATH`, `EMBEDDING_MODEL`), or a `DATABASE_*` key in
  `solidvision.env` is ignored and named in a warning in the log.
- A value that does not validate stops the process, with one line on stderr
  that names the file and the key, and no traceback.
- Write paths without quotes, spaces included. Double quotes turn the `\f`,
  `\n`, `\r` and `\t` of a Windows path into control characters, and the path
  is refused. So are `%NAME%` (write `${NAME}`), `D:thumbs`, `\thumbs` and an
  empty value. A relative path is relative to the folder of the file it is
  written in.

### Where the user's data is
| Mode | Data directory |
|---|---|
| installed | `%LOCALAPPDATA%\SolidVision` |
| development | `%LOCALAPPDATA%\SolidVision-dev` |

```text
%LOCALAPPDATA%\SolidVision\
    solidvision.env    optional; never created by the app
    logs\              one file per program: uvicorn.log, job_runner.log, ...
    thumbnails\
    postgres\          the embedded cluster, its password and its logs
    cache\             what the model libraries write; deletable (RFC-035)
```
`THUMBNAIL_DIRECTORY`, `LOG_DIRECTORY` and `EMBEDDED_POSTGRES_DIRECTORY` move
each of the three. The data directory itself is not configurable.

Each program writes its own log file, named after the module given to `-m`
(`uvicorn.log`, `job_runner.log`, `embedded.log`, `pytest.log`), because two
processes rotating one file lose records on Windows. `LOG_FILENAME` gives every
process one fixed name instead; two processes that share a file do not rotate
it while both are running.

### If you already had a checkout
The development defaults moved from `SolidVision` to `SolidVision-dev`. The
code moves and deletes nothing.
- **Thumbnails** stay in `%LOCALAPPDATA%\SolidVision\thumbnails`. Move the
  folder to `SolidVision-dev\thumbnails`, or run the thumbnail backfill.
- **A cluster** the checkout created in `%LOCALAPPDATA%\SolidVision\postgres`
  is where an installed app will look for its own. Stop it and move it to
  `SolidVision-dev\postgres`, or delete it.
- **The log** goes to `SolidVision-dev\logs` unless the `.env` has
  `LOG_DIRECTORY`. `LOG_DIRECTORY=logs` still means `<repository>\logs`.

### Development without Docker
One line in the `.env`:
```
DATABASE_SOURCE=embedded
```
The API, the job runner and the CLI then use the embedded cluster of
`SolidVision-dev`, which has to be up (`... embedded start`, below). With
`external`, the default in a checkout, nothing changes: `DATABASE_*` and the
container.

### Running the code as an installed app
`scripts/stage_installation.py` copies the installed tree to a directory
outside the repository, without `pyproject.toml`, `.env` and the tests:
```powershell
python scripts/stage_installation.py C:\stage\SolidVision --read-only
$env:PYTHONPATH = "C:\stage\SolidVision\backend"
$env:LOCALAPPDATA = "C:\stage\profile"      # leave the real profile alone
python -m app.infrastructure.database.embedded start
python -m uvicorn app.presentation.api:app
python scripts/stage_installation.py C:\stage\SolidVision --remove
```
`--read-only` locks the tree as `C:\Program Files` is locked to an ordinary
process. It is not the installer: the interpreter is the virtualenv's.

## Embedded PostgreSQL (RFC-033)

The default development database is still the one in `docker-compose.yml`, and
a plain `pytest` still goes to it through the `.env`. The embedded cluster is
the same PostgreSQL 16.15 and pgvector 0.8.6 without Docker, as a child
process. Windows only.

### Materialize the runtime, once per checkout
```bash
python scripts/vendor_postgres.py
```
This downloads the 16 packages `scripts/postgres-win-64.lock` pins, checks the
`md5` of each, and leaves 123 MB in `runtime/postgres`, which is outside git.
Nothing is installed on the machine and no solver runs. `--lock` re-solves the
three pins of RFC-033a and rewrites the lockfile; it is how the lockfile is
changed, not something a checkout needs.

### Start, stop, and run a command against it
From the repository root, with `backend` on the path. In PowerShell:
```powershell
$env:PYTHONPATH = "backend"
python -m app.infrastructure.database.embedded start
python -m app.infrastructure.database.embedded status
python -m app.infrastructure.database.embedded run -- python -m pytest
python -m app.infrastructure.database.embedded stop
```
The first `start` creates the cluster, which takes tens of seconds on a hard
disk, creates the `solidvision` database and applies the migrations. Later
starts take about a second.

`run -- <command>` starts the cluster, runs the command with
`DATABASE_SOURCE=embedded`, `EMBEDDED_POSTGRES_DIRECTORY` and
`EMBEDDED_POSTGRES_RUNTIME_DIRECTORY` set for it, and returns the command's
exit code. It leaves the cluster as it found it: a cluster it started is
stopped at the end, and one that was already up is left up. Neither the port
nor the password is exported. The port is chosen at start and can change at
the next one, so the command's engine reads both from the cluster whenever it
opens a connection.

### Where it lives
| Setting | Default | What it is |
|---|---|---|
| `EMBEDDED_POSTGRES_RUNTIME_DIRECTORY` | `runtime\postgres` under the root | The binaries. Never taken from `PATH`. |
| `EMBEDDED_POSTGRES_DIRECTORY` | `postgres` under the data directory | The cluster, its password and its logs, per user. |

The cluster is in `postgres\16\data`; the server's log is in `data\log`, one
file per weekday, and `postgres\16\startup.log` holds what the server says
before its logging starts. A checkout's cluster and an installed app's are
apart by default, because the two data directories are.

### Its tests
```bash
pytest -m embedded
```
These start real servers in temporary directories and are deselected by
default, like `slow`. Without the runtime they are skipped with the command
above, not failed. The unit tests of the adapter need no runtime and run in
the default suite.

## The models, offline (RFC-035)

CLIP (`laion/CLIP-ViT-B-32-laion2B-s34B-b79K`) and the Portuguese-to-English
translator (`Helsinki-NLP/opus-mt-ROMANCE-en`) are loaded from a directory,
never by name and never from the network, in both modes. Which models these
are -- the repository, the commit, and the size and `sha256` of every file -- is
`backend/app/infrastructure/ai/models.lock.json`, which is code: changing a
model is a commit, with a migration and a reindexing plan, not a setting.

### Materialize them, once per checkout
```bash
python scripts/vendor_models.py --from-cache   # from your Hugging Face cache
python scripts/vendor_models.py                # or download, at the pinned commits
```
Either way every file is checked against the lockfile, and 881 MB land in
`runtime/models`, outside git. `--from-cache` reads the snapshots a load by
name left in `~\.cache\huggingface`, and refuses one whose hash is not the
lockfile's. `--lock` rewrites the lockfile from those snapshots, checking each
file against what the Hub declares; it is how the lockfile is changed, not
something a checkout needs. `MODEL_RUNTIME_DIRECTORY` points a checkout at
another tree -- several checkouts can share one -- and only an identical copy
is accepted.

### What the app checks
The translator's weights exist on the Hub only as a pickle,
`pytorch_model.bin`; `vendor_models.py` checks its hash, converts it to
`model.safetensors` (the same tensors, the same bytes every time) and leaves no
pickle in the tree, so the app never unpickles anything.

Every process that loads a model checks, when it starts, that the tree is
there and every file has its size; and before it loads a model, the `sha256`
of that model's files. A file already hashed is
vouched for afterwards by its size, date and NTFS file id, kept in
`<data>\cache\models-verified.json`. A model that cannot be loaded is one line
that names the file and what to do: the job runner and the CLI print it and
exit with 1; the API starts anyway, and search answers 503 with it.

The package `app.infrastructure.ai` sets `HF_HUB_OFFLINE=1`, `HF_HOME` and
`TORCHINDUCTOR_CACHE_DIR` (both under `<data>\cache`) before anything imports
`transformers`, overwriting whatever the environment had. Import nothing that
imports `transformers` or `huggingface_hub` before it.

`WARM_UP_MODELS` defaults to on installed and off in a checkout.

### Their tests
```bash
pytest -m slow
```
The `slow` tests load the real models from `runtime/models`, and are skipped
without it, with the command above. One of them compares every load with the
vectors the load by name produced before RFC-035, kept in
`tests/fixtures/rfc035_reference_embeddings.npz`: bit for bit on the machine
that wrote it, within a tolerance elsewhere.
