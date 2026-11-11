"""RFC-050 section 2 pilot: the default suite in a clone set up like the runner.

    .venv\\Scripts\\python.exe experiments/rfc-050-suite-do-backend/pilot_suite.py [commit]

What the runner will have, as far as this machine can give it:

- a clone of `commit` (default `origin/frontend`) made with
  `core.autocrlf=true`, without `.env` and without `runtime\\models`;
- `runtime\\postgres` copied into the clone, as the cache will put it there;
- `TEMP` and `TMP` on an 8.3 short path, as the runner's are
  (`C:\\Users\\RUNNER~1\\...`, because `runneradmin` is longer than eight
  characters), when this volume makes short names;
- the image's three PostgreSQL variables (`PGBIN`, `PGDATA`, `PGROOT`);
- the cluster of `embedded run --` in a new folder, never the development one.

Then `embedded run -- python scripts/check.py pytest`, by this repository's
venv (the clone's lockfiles are the same), every line timestamped. Writes
`pilot_suite.json` beside this file, or `pilot_suite-<commit>.json` when a
commit is given (the reference of section 9, criterion 1); the `.log` stays
out of git.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
VENV_PYTHON = REPO / ".venv" / "Scripts" / "python.exe"
RUNTIME = REPO / "runtime" / "postgres"

RUNNER_PG = {
    "PGBIN": r"C:\Program Files\PostgreSQL\17\bin",
    "PGDATA": r"C:\PostgreSQL\17\data",
    "PGROOT": r"C:\Program Files\PostgreSQL\17",
}
"""Windows2025-Readme.md, image 20261101: the service is installed and disabled."""


def short_path(path: Path) -> str:
    buffer = ctypes.create_unicode_buffer(1024)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 1024)
    return buffer.value if length else str(path)


def git(*arguments: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


SUMMARY = re.compile(r"^=+ (?P<counts>.*?) in (?P<seconds>[\d.]+)s")
MARKS = {
    "creating": "creating the cluster",
    "starting": "starting the server",
    "ready": "ready on ",
    "migrating": "applying migrations",
    "database": "embedded postgres: database ",
    "pytest_begins": "==> pytest",
    "pytest_ends": "<== pytest",
}


def main() -> int:
    commit = sys.argv[1] if len(sys.argv) > 1 else "origin/frontend"
    sha = git("rev-parse", commit, cwd=REPO)
    name = "pilot_suite" if len(sys.argv) == 1 else f"pilot_suite-{sha[:7]}"
    work = Path(tempfile.mkdtemp(prefix="rfc050-suite-"))
    clone = work / "repo"
    temp_long = work / "runneradmin-temporary"
    temp_long.mkdir()
    temp = short_path(temp_long)
    results: dict[str, Any] = {
        "commit": sha,
        "temp": {"long": str(temp_long), "short": temp, "is_short": temp != str(temp_long)},
    }
    try:
        started = time.perf_counter()
        git("clone", "-q", "-c", "core.autocrlf=true", "--no-checkout", str(REPO), str(clone))
        git("checkout", "-q", sha, cwd=clone)
        results["clone_seconds"] = round(time.perf_counter() - started, 1)
        started = time.perf_counter()
        shutil.copytree(RUNTIME, clone / "runtime" / "postgres")
        results["copy_runtime_seconds"] = round(time.perf_counter() - started, 1)
        assert not (clone / ".env").exists() and not (clone / "runtime" / "models").exists()

        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith(("PG", "DATABASE_", "EMBEDDED_"))
        }
        environment |= RUNNER_PG | {
            "TEMP": temp,
            "TMP": temp,
            "PYTHONPATH": str(clone / "backend"),
            "EMBEDDED_POSTGRES_DIRECTORY": str(work / "cluster"),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
        command = [
            str(VENV_PYTHON), "-m", "app.infrastructure.database.embedded",
            "run", "--", str(VENV_PYTHON), "scripts/check.py", "pytest",
        ]
        log = HERE / f"{name}.log"
        marks: dict[str, float] = {}
        tail: list[str] = []
        started = time.perf_counter()
        with log.open("w", encoding="utf-8") as out:
            process = subprocess.Popen(
                command,
                cwd=clone,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            assert process.stdout is not None
            for line in process.stdout:
                now = time.perf_counter() - started
                out.write(f"{now:8.1f} {line}")
                for mark, text in MARKS.items():
                    if text in line and mark not in marks:
                        marks[mark] = round(now, 1)
                tail.append(line.rstrip())
                tail = tail[-200:]
            code = process.wait()
        results["exit_code"] = code
        results["wall_seconds"] = round(time.perf_counter() - started, 1)
        results["marks_seconds"] = marks
        for line in tail:
            found = SUMMARY.match(line)
            if found:
                results["pytest_summary"] = found["counts"]
                results["pytest_seconds"] = float(found["seconds"])
        results["short_summary"] = [
            line for line in tail if line.startswith(("FAILED", "SKIPPED", "ERROR", "XFAIL"))
        ]
    finally:
        shutil.rmtree(work, ignore_errors=True)
    out_json = HERE / f"{name}.json"
    out_json.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
