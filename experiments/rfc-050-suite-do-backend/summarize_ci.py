"""RFC-050 sections 15.4 and 15.5: the tables, from measure_ci.json.

    python experiments/rfc-050-suite-do-backend/summarize_ci.py

The cold run is the first of `medicao/rfc-050-fria` (37dab90, no cache in
that branch). The warm ones are every run of `medicao/rfc-050-quente` whose
venv and runtime were restored. The ten of criterion 2 are the empty commits
("Execução N de 10"). The steps inside the composite action come from the
log's groups, in their order: the base Python, restoring the venv,
`make_venv.py` and saving it only when the venv was not found.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

DATA = json.loads(Path(__file__).with_name("measure_ci.json").read_text(encoding="utf-8"))


def job(run: dict[str, Any], name: str) -> dict[str, Any]:
    return next(j for j in run["jobs"] if j["name"] == name)


def group(entry: dict[str, Any], starts: str, nth: int = 0) -> float | None:
    found = [s for title, s in entry.get("log", {}).get("groups", []) if title.startswith(starts)]
    return found[nth] if len(found) > nth else None


def step(entry: dict[str, Any], has: str) -> float | None:
    found = [s["s"] for s in entry["steps"] if has in s["name"] and s["conclusion"] == "success"]
    return found[0] if found else None


def row(run: dict[str, Any]) -> dict[str, Any]:
    backend, estatico, frontend = (job(run, n) for n in ("backend", "estatico", "frontend"))
    phases = backend.get("log", {}).get("phases", {})
    pytest = backend.get("log", {}).get("pytest", {})
    return {
        "sha": run["sha"],
        "conclusion": run["conclusion"],
        "attempt": run["attempt"],
        "push_to_result": run["push_to_result_s"],
        "backend": backend["s"],
        "estatico": estatico["s"],
        "frontend": frontend["s"],
        "python_backend": group(backend, "Run $folder"),
        "python_estatico": group(estatico, "Run $folder"),
        "venv_restore": group(backend, "Run actions/cache/restore", 0),
        "make_venv": group(backend, "Run & $env:BASE_PYTHON scripts/make_venv.py"),
        "runtime_restore": step(backend, "actions/cache/restore"),
        "vendor_postgres": step(backend, "vendor_postgres.py"),
        "initdb": phases.get("initdb"),
        "pytest_step": phases.get("pytest"),
        "pytest_itself": pytest.get("s"),
        "pytest_counts": pytest.get("counts"),
        "run_step": step(backend, "embedded run"),
    }


def spread(values: list[float]) -> str:
    values = [v for v in values if v is not None]
    if not values:
        return "-"
    return f"{min(values)} to {max(values)}, median {statistics.median(values)}"


def main() -> None:
    cold = row(DATA["branches"]["medicao/rfc-050-fria"][0])
    warm_runs = DATA["branches"]["medicao/rfc-050-quente"]
    ten = [row(r) for r in warm_runs if r["message"].startswith("Execução ")]
    print("criterion 2:", len(ten), "runs,", sum(r["conclusion"] == "success" for r in ten),
          "green, attempts", sorted({r["attempt"] for r in ten}))
    for r in ten:
        print(f"  {r['sha']} {r['conclusion']} push->result {r['push_to_result']} s, backend {r['backend']} s, "
              f"pytest {r['pytest_counts']}")
    print("\nstep: cold | the ten (min to max, median)")
    for key in (
        "python_backend", "python_estatico", "venv_restore", "make_venv", "runtime_restore",
        "vendor_postgres", "initdb", "pytest_step", "pytest_itself", "run_step",
        "backend", "estatico", "frontend", "push_to_result",
    ):
        print(f"  {key:<16} {cold[key]!s:>7} | {spread([r[key] for r in ten])}")
    out = Path(__file__).with_name("summarize_ci.json")
    out.write_text(json.dumps({"cold": cold, "ten": ten}, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")


if __name__ == "__main__":
    main()
