"""RFC-048 section 10: the command in a fresh clone, and one defect per step.

    python experiments/rfc-048-conferencias/measure_check.py \\
        --python C:\\<Python 3.12.9>\\python.exe --source <repository or clone>

Run it with any Python 3.10+; it never imports the project. What it does,
each step timed and written to `measure_check.json` beside this file (the
console output also goes to `measure_check.log`, outside git):

1. `git clone -c core.autocrlf=true` of `--source` into a new folder of
   `%TEMP%`, and counts the CRLF files of `frontend/` and `backend/`.
2. `scripts/make_venv.py` of the clone, by `--python`, with `PIP_CACHE_DIR`
   on an empty folder: the cold-cache time.
3. `npm ci` in the clone's `frontend/`, `ELECTRON_SKIP_BINARY_DOWNLOAD=1`.
4. `scripts/vendor_postgres.py` of the clone, by the clone's venv.
5. The whole command, inside `embedded run --`, with the cluster in a
   temporary folder (`EMBEDDED_POSTGRES_DIRECTORY`) and never the
   development one. No `.env`, no `runtime\\models`: as on a runner.
6. One planted defect at a time (section 10's table), each undone before
   the next: the command must fail, and its first red step must be the
   defect's. The pytest defect runs inside `embedded run --`; the others
   stop before pytest, and run against the stopped temporary cluster.
7. `check.py pytest` against the stopped temporary cluster: it must fail at
   the test connection, in under 30 s, without starting the suite.
"""

from __future__ import annotations

import argparse
import datetime
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
RESULT = HERE / "measure_check.json"
LOG = HERE / "measure_check.log"

SUMMARY_LINE = re.compile(r"^  (\w+)\s+(passed|FAILED)\s+([\d.]+) s$")
PYTEST_TAIL = re.compile(r"^=+ (.+) in ([\d.]+)s")


class Log:
    def __init__(self, path: Path) -> None:
        self.handle = path.open("w", encoding="utf-8")

    def say(self, text: str) -> None:
        print(text, flush=True)
        self.handle.write(text + "\n")
        self.handle.flush()


def run(
    log: Log,
    command: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 7200,
) -> tuple[int, str, float]:
    """Run, tee the output to the log, and return (code, output, seconds)."""
    log.say(f"$ {' '.join(command)}  (in {cwd})")
    started = time.perf_counter()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    lines = []
    for line in process.stdout:
        lines.append(line)
        log.handle.write(line)
    code = process.wait(timeout=timeout)
    seconds = time.perf_counter() - started
    log.handle.flush()
    log.say(f"  -> exit {code} in {seconds:.1f} s")
    return code, "".join(lines), seconds


def parse_check(output: str) -> dict[str, Any]:
    steps = {}
    for line in output.splitlines():
        found = SUMMARY_LINE.match(line)
        if found:
            steps[found[1]] = {"passed": found[2] == "passed", "s": float(found[3])}
    first_red = next(
        (
            line[4:].split(":")[0]
            for line in output.splitlines()
            if line.startswith("<== ")
            and line.rstrip().endswith(" s")
            and ": FAILED in " in line
        ),
        None,
    )
    pytest_tail = None
    for line in output.splitlines():
        found = PYTEST_TAIL.match(line.strip())
        if found and ("passed" in found[1] or "failed" in found[1]):
            pytest_tail = {"counts": found[1], "s": float(found[2])}
    return {
        "steps": steps,
        "first_red": first_red,
        "pytest": pytest_tail,
        "suite_started": "test session starts" in output,
    }


def crlf_files(clone: Path, folder: str) -> int:
    listed = subprocess.run(
        ["git", "ls-files", "--eol", folder],
        cwd=clone,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sum("w/crlf" in line for line in listed.splitlines())


def replace_in(path: Path, old: str, new: str) -> None:
    text = path.read_bytes().decode("utf-8")
    assert old in text, f"{old!r} not in {path}"
    path.write_bytes(text.replace(old, new, 1).encode("utf-8"))


def append_to(path: Path, text: str) -> None:
    content = path.read_bytes()
    newline = b"\r\n" if b"\r\n" in content else b"\n"
    path.write_bytes(content + text.replace("\n", newline.decode()).encode())


# --------------------------------------------------------------------------
# The defects of section 10, one per step
# --------------------------------------------------------------------------


ESLINT_DEFECT = """import { useState } from "react";

export function Planted({ on }: { on: boolean }): number {
  if (on) {
    const [value] = useState(0);
    return value;
  }
  return 0;
}
"""

TSC_DEFECT = """function shout(word: string): string {
  return word.toUpperCase();
}

export const planted = shout(42);
"""

VITEST_DEFECT = """import { expect, test } from "vitest";

test("planted by RFC-048", () => {
  expect(1 + 1).toBe(3);
});
"""


def plant(clone: Path, venv_python: Path, step: str, log: Log) -> str:
    """Make the defect of `step` in the clone; return what was done."""
    if step == "env":
        run(
            log,
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--disable-pip-version-check",
                "sqlalchemy==2.1.1",
            ],
            clone,
        )
        return "sqlalchemy 2.1.1 installed over the lockfile's 2.0.51"
    if step == "ruff":
        path = clone / "backend" / "app" / "domain" / "value_objects" / "device_id.py"
        replace_in(
            path,
            "from __future__ import annotations",
            "from __future__ import annotations\n\nimport json",
        )
        return "an unused `import json`, in backend/app/.../device_id.py"
    if step == "black":
        append_to(clone / "scripts" / "pe_imports.py", "\nPLANTED = {  'a':1 }\n")
        return "`PLANTED = {  'a':1 }` at the end of scripts/pe_imports.py"
    if step == "mypy":
        append_to(
            clone / "backend" / "app" / "domain" / "value_objects" / "device_id.py",
            '\n\ndef _planted() -> int:\n    return "not an int"\n',
        )
        return "a function annotated `-> int` returning a str, in backend/app"
    if step == "eslint":
        (clone / "frontend" / "src" / "planted.tsx").write_text(
            ESLINT_DEFECT, encoding="utf-8", newline="\n"
        )
        return "useState inside an `if`, in frontend/src/planted.tsx"
    if step == "tsc":
        (clone / "frontend" / "src" / "planted.ts").write_text(
            TSC_DEFECT, encoding="utf-8", newline="\n"
        )
        return "shout(42) where shout takes a string, in frontend/src/planted.ts"
    if step == "prettier":
        path = clone / "frontend" / "src" / "main.tsx"
        content = path.read_bytes()
        assert b"\r\n" not in content
        path.write_bytes(content.replace(b"\n", b"\r\n"))
        return "frontend/src/main.tsx rewritten with CRLF"
    if step == "vitest":
        (clone / "frontend" / "src" / "planted.test.ts").write_text(
            VITEST_DEFECT, encoding="utf-8", newline="\n"
        )
        return "expect(1 + 1).toBe(3), in frontend/src/planted.test.ts"
    if step == "pytest":
        append_to(
            clone / "backend" / "tests" / "domain" / "test_image_id.py",
            "\n\ndef test_planted_by_rfc_048() -> None:\n    assert 1 + 1 == 3\n",
        )
        return "assert 1 + 1 == 3, in backend/tests/domain/test_image_id.py"
    raise AssertionError(step)


def undo(clone: Path, venv_python: Path, step: str, log: Log) -> None:
    if step == "env":
        lock = (clone / "scripts" / "python-win-64.lock").read_text(encoding="utf-8")
        line = next(x for x in lock.splitlines() if x.startswith("sqlalchemy==="))
        pinned = clone.parent / "sqlalchemy.txt"
        pinned.write_text(f"--index-url https://pypi.org/simple\n{line}\n")
        run(
            log,
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--require-hashes",
                "--disable-pip-version-check",
                "-r",
                str(pinned),
            ],
            clone,
        )
        return
    subprocess.run(["git", "checkout", "--", "."], cwd=clone, check=True)
    subprocess.run(["git", "clean", "-fdq", "frontend/src"], cwd=clone, check=True)
    left = subprocess.run(
        ["git", "status", "--porcelain"], cwd=clone, capture_output=True, text=True
    ).stdout
    assert left.strip() == "", left


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--work", type=Path, default=None)
    args = parser.parse_args()

    work = args.work or Path(
        tempfile.mkdtemp(prefix="solidvision-rfc048-", dir=os.environ.get("TEMP"))
    )
    work.mkdir(parents=True, exist_ok=True)
    clone = work / "clone"
    cluster = work / "cluster"
    pip_cache = work / "pip-cache"
    log = Log(LOG)
    result: dict[str, Any] = {
        "rfc": "048",
        "date": datetime.date.today().isoformat(),
        "machine": "NVMe, Windows 11 (the RFC-048 pilot's)",
        "work": str(work),
    }
    base = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DATABASE_", "EMBEDDED_POSTGRES", "PYTHON", "PG"))
        and key not in ("WARM_UP_MODELS", "VIRTUAL_ENV")
    }

    # 1. The clone
    code, _, seconds = run(
        log,
        ["git", "clone", "-q", "-c", "core.autocrlf=true", args.source, str(clone)],
        work,
    )
    assert code == 0
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=clone, capture_output=True, text=True
    ).stdout.strip()
    result["clone"] = {
        "commit": commit,
        "s": round(seconds, 1),
        "crlf_files_frontend": crlf_files(clone, "frontend"),
        "crlf_files_backend": crlf_files(clone, "backend"),
        "env_file": (clone / ".env").exists(),
        "runtime_models": (clone / "runtime" / "models").exists(),
    }
    log.say(json.dumps(result["clone"]))

    # 2. The venv, with the pip cache cold
    pip_cache.mkdir()
    code, output, seconds = run(
        log,
        [str(args.python), "scripts/make_venv.py"],
        clone,
        {**base, "PIP_CACHE_DIR": str(pip_cache)},
    )
    assert code == 0, "make_venv.py failed"
    venv_python = clone / ".venv" / "Scripts" / "python.exe"
    result["make_venv"] = {
        "python": subprocess.run(
            [str(venv_python), "-c", "import sys; print(sys.version.split()[0])"],
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "pip_cache": "cold (empty PIP_CACHE_DIR)",
        "s": round(seconds, 1),
        "said": [x for x in output.splitlines() if x.startswith(str(clone))],
    }

    # 3. npm ci
    npm = shutil.which("npm")
    assert npm is not None
    code, _, seconds = run(
        log,
        [npm, "ci", "--no-audit", "--no-fund"],
        clone / "frontend",
        {**base, "ELECTRON_SKIP_BINARY_DOWNLOAD": "1"},
    )
    assert code == 0
    node = subprocess.run(["node", "--version"], capture_output=True, text=True)
    result["npm_ci"] = {
        "s": round(seconds, 1),
        "node": node.stdout.strip(),
        "npm_cache": "this machine's, warm",
    }

    # 4. The PostgreSQL runtime of the clone
    code, _, seconds = run(
        log, [str(venv_python), "scripts/vendor_postgres.py"], clone, base
    )
    assert code == 0
    result["vendor_postgres"] = {"s": round(seconds, 1)}

    # 5. The whole command, inside `embedded run --`
    embedded = {
        **base,
        "PYTHONPATH": "backend",
        "EMBEDDED_POSTGRES_DIRECTORY": str(cluster),
    }
    around = [
        str(venv_python),
        "-m",
        "app.infrastructure.database.embedded",
        "run",
        "--",
    ]
    check = [str(venv_python), "scripts/check.py"]
    code, output, seconds = run(log, [*around, *check], clone, embedded)
    result["whole"] = {
        "exit": code,
        "wall_s": round(seconds, 1),
        "mypy_cache": "cold (a fresh clone)",
        **parse_check(output),
    }
    log.say(json.dumps(result["whole"], indent=1))

    # A second run, the caches warm
    code, output, seconds = run(log, [*around, *check], clone, embedded)
    result["whole_warm"] = {
        "exit": code,
        "wall_s": round(seconds, 1),
        **parse_check(output),
    }

    # 6. One defect per step
    stopped = {
        **base,
        "DATABASE_SOURCE": "embedded",
        "EMBEDDED_POSTGRES_DIRECTORY": str(cluster),
    }
    defects = []
    for step in (
        "env",
        "ruff",
        "black",
        "mypy",
        "eslint",
        "tsc",
        "prettier",
        "vitest",
        "pytest",
    ):
        what = plant(clone, venv_python, step, log)
        if step == "pytest":
            code, output, seconds = run(log, [*around, *check], clone, embedded)
        else:
            code, output, seconds = run(log, check, clone, stopped)
        parsed = parse_check(output)
        defects.append(
            {
                "step": step,
                "defect": what,
                "exit": code,
                "first_red": parsed["first_red"],
                "as_expected": code == 1 and parsed["first_red"] == step,
                "wall_s": round(seconds, 1),
                "pytest": parsed["pytest"],
            }
        )
        log.say(json.dumps(defects[-1]))
        undo(clone, venv_python, step, log)
    result["defects"] = defects

    # 7. The database stopped
    code, output, seconds = run(log, [*check, "pytest"], clone, stopped)
    parsed = parse_check(output)
    result["database_stopped"] = {
        "exit": code,
        "first_red": parsed["first_red"],
        "wall_s": round(seconds, 1),
        "suite_started": parsed["suite_started"],
        "said": [x for x in output.splitlines() if "database" in x.lower()][:3],
        "under_30_s": seconds < 30,
    }
    log.say(json.dumps(result["database_stopped"]))

    RESULT.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    log.say(f"written: {RESULT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
