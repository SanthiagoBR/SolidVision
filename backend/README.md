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

## Embedded PostgreSQL (RFC-033)

The development database is still the one in `docker-compose.yml`, and a plain
`pytest` still goes to it through the `.env`. The embedded cluster is the same
PostgreSQL 16.15 and pgvector 0.8.6 without Docker, as a child process, and is
optional until the launcher of RFC-036. Windows only.

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

`run -- <command>` starts the cluster, runs the command with `DATABASE_HOST`,
`DATABASE_PORT`, `DATABASE_USER`, `DATABASE_PASSWORD` and `DATABASE_NAME` set
for it, and returns the command's exit code. It leaves the cluster as it found
it: a cluster it started is stopped at the end, and one that was already up is
left up. The port is chosen at start and the password at creation, so nothing
else knows them: reach the cluster through `run`, not through the `.env`.

### Where it lives
| Setting | Default | What it is |
|---|---|---|
| `EMBEDDED_POSTGRES_RUNTIME_DIRECTORY` | `runtime/postgres` under the repository | The binaries. Never taken from `PATH`. |
| `EMBEDDED_POSTGRES_DIRECTORY` | `%LOCALAPPDATA%\SolidVision\postgres` | The cluster, its password and its logs, per user. |

The cluster is in `postgres\16\data`; the server's log is in `data\log`, one
file per weekday, and `postgres\16\startup.log` holds what the server says
before its logging starts. Set `EMBEDDED_POSTGRES_DIRECTORY` to keep a
checkout's cluster apart from an installed app's.

### Its tests
```bash
pytest -m embedded
```
These start real servers in temporary directories and are deselected by
default, like `slow`. Without the runtime they are skipped with the command
above, not failed. The unit tests of the adapter need no runtime and run in
the default suite.
