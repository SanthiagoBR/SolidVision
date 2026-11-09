"""RFC-049 pilot (section 2.4): what a cache of the pip cache saves, against a
cache of the whole venv.

    python experiments/rfc-049-primeiro-workflow/pilot_venv_cache.py <empty folder>

A: make_venv.py with an empty PIP_CACHE_DIR (cold), phase by phase.
B: make_venv.py again, the same PIP_CACHE_DIR now full (warm): the control.
C: B's venv packed with tar --zstd (the actions/cache format), deleted,
   unpacked back to the same path, and checked by `check.py ruff` (env + ruff).

Needs the Python 3.12.9 at %LOCALAPPDATA%\\Programs\\Python\\3.12.9 (RFC-048
section 17.4) and Windows' own tar (bsdtar, with zstd). Writes pilot.json in
the folder, and leaves the pip cache there; the venvs are deleted.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PY = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Python" / "3.12.9" / "python.exe"
WORK = Path(sys.argv[1])
OUT = WORK / "pilot.json"


def size_and_files(folder: Path) -> tuple[float, int]:
    total = 0
    files = 0
    for root, _dirs, names in os.walk(folder):
        for name in names:
            try:
                total += (Path(root) / name).stat().st_size
                files += 1
            except OSError:
                pass
    return round(total / 2**20, 1), files


def make_venv(target: Path, cache: Path) -> dict:
    env = {**os.environ, "PIP_CACHE_DIR": str(cache), "PYTHONIOENCODING": "utf-8"}
    started = time.perf_counter()
    marks: list[tuple[float, str]] = []
    process = subprocess.Popen(
        [str(PY), str(REPO / "scripts" / "make_venv.py"), str(target)],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    for line in process.stdout:
        line = line.rstrip()
        if line[:4] in ("1/4 ", "2/4 ", "3/4 ", "4/4 ") or "packages of the lockfiles" in line:
            marks.append((round(time.perf_counter() - started, 1), line[:90]))
    code = process.wait()
    total = round(time.perf_counter() - started, 1)
    phases = {}
    for (t0, label), (t1, _) in zip(marks, [*marks[1:], (total, "end")]):
        phases[label[:3]] = round(t1 - t0, 1)
    return {"exit": code, "s": total, "marks": marks, "phases": phases}


def timed(command: list[str], cwd: Path | None = None, env: dict | None = None) -> dict:
    started = time.perf_counter()
    completed = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
    )
    return {
        "exit": completed.returncode,
        "s": round(time.perf_counter() - started, 1),
        "tail": (completed.stdout + completed.stderr).strip().splitlines()[-6:],
    }


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    cache = WORK / "pip-cache"
    result: dict = {"date": time.strftime("%Y-%m-%d %H:%M"), "python": str(PY)}

    a = WORK / "venv-a"
    result["A_cold"] = make_venv(a, cache)
    result["pip_cache_mb_files"] = size_and_files(cache)
    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    shutil.rmtree(a, ignore_errors=True)

    b = WORK / "venv-b"
    result["B_warm"] = make_venv(b, cache)
    result["venv_mb_files"] = size_and_files(b)
    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    archive = WORK / "venv.tar.zst"
    result["C_pack"] = timed(["tar", "--zstd", "-cf", str(archive), "-C", str(WORK), "venv-b"])
    result["C_archive_mb"] = round(archive.stat().st_size / 2**20, 1)
    started = time.perf_counter()
    shutil.rmtree(b)
    result["C_delete_s"] = round(time.perf_counter() - started, 1)
    result["C_unpack"] = timed(["tar", "--zstd", "-xf", str(archive), "-C", str(WORK)])
    venv_python = b / "Scripts" / "python.exe"
    result["C_check_ruff"] = timed(
        [str(venv_python), "scripts/check.py", "ruff"], cwd=REPO,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    shutil.rmtree(b, ignore_errors=True)
    archive.unlink(missing_ok=True)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
