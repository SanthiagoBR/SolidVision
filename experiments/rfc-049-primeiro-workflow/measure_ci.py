"""RFC-049 section 8: the workflow's runs, read from GitHub's API.

    python experiments/rfc-049-primeiro-workflow/measure_ci.py <branch>... > measure_ci.log

For each branch, every run of ci.yml on it, oldest first: the run's
`created_at`, and the `started_at` and `completed_at` of each job and of each
step, with their conclusions. "Push to result" is the run's `created_at` to
the last job's `completed_at`.

The logs need a token, even on a public repository (the API answers 403
without one): with `GITHUB_TOKEN` set, each job's log is read too, and from
it check.py's summary (each step, passed or FAILED, and its seconds), the
`env` step's problems, vitest's count, and what each cache step did. Without
a token, only the times.

Adds the branches to measure_ci.json beside this file (a branch read again
replaces its entry), and prints the log.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

API = "https://api.github.com"
REPOSITORY = "SanthiagoBR/SolidVision"
WORKFLOW = "ci.yml"
OUT = Path(__file__).with_name("measure_ci.json")
TOKEN = os.environ.get("GITHUB_TOKEN") or None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A log is a redirect to storage that must not get the GitHub token."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def get(url: str, *, authorized: bool = True) -> bytes:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "solidvision-rfc-049-measure",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN and authorized:
        headers["Authorization"] = f"Bearer {TOKEN}"
    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(request, timeout=60) as response:
            body: bytes = response.read()
            return body
    except urllib.error.HTTPError as error:
        location = error.headers.get("Location")
        if error.code in (301, 302, 303, 307, 308) and location:
            return get(location, authorized=False)
        raise


def api(path: str) -> Any:
    return json.loads(get(f"{API}/repos/{REPOSITORY}/{path}"))


def when(stamp: str | None) -> datetime | None:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else None


def seconds(start: str | None, end: str | None) -> float | None:
    begun, ended = when(start), when(end)
    if begun is None or ended is None:
        return None
    return round((ended - begun).total_seconds(), 1)


# --------------------------------------------------------------------------
# What a log says
# --------------------------------------------------------------------------

STAMP = re.compile(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ?")
COLOR = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
"""vitest colors its output when it sees `CI`."""
SUMMARY_LINE = re.compile(
    r"^\s+(?P<name>\w+)\s+(?P<verdict>passed|FAILED|not run)\s*(?:(?P<s>[\d.]+) s)?$"
)
VITEST = re.compile(r"^\s*(?P<what>Test Files|Tests)\s+(?P<counts>.+)$")
CACHE = re.compile(
    r"(Cache restored from key|Cache not found|Cache saved|Cache Size|"
    r"Unable to reserve cache|Failed to save|Path Validation Error|"
    r"Successfully set up CPython|Attempting to download|Found in cache)"
)


def read_log(text: str) -> dict[str, Any]:
    lines = [COLOR.sub("", STAMP.sub("", line)) for line in text.splitlines()]
    found: dict[str, Any] = {"summary": [], "verdict": None, "env": [], "vitest": {}}
    found["caches"] = [line.strip() for line in lines if CACHE.search(line)][:30]
    in_summary = False
    for line in lines:
        if line.strip() == "summary:":
            in_summary, found["summary"] = True, []
            continue
        if in_summary:
            step = SUMMARY_LINE.match(line)
            if step and step["name"] != "total":
                found["summary"].append(
                    [step["name"], step["verdict"], float(step["s"] or 0)]
                )
                continue
            if line.startswith("FAILED: ") or line.startswith("every step passed"):
                found["verdict"] = line.strip()
                in_summary = False
                continue
            if not line.strip().startswith("total"):
                in_summary = False
        if line.startswith("env: ") and " packages of " not in line:
            found["env"].append(line.strip())
        vitest = VITEST.match(line)
        if vitest:
            found["vitest"][vitest["what"]] = vitest["counts"].strip()
        if re.search(r"^\s*(FAIL|×) ", line) and len(found.get("failing", [])) < 10:
            found.setdefault("failing", []).append(line.strip())
    reds = [name for name, verdict, _ in found["summary"] if verdict == "FAILED"]
    found["first_red"] = reds[0] if reds else None
    return found


# --------------------------------------------------------------------------
# Runs and jobs
# --------------------------------------------------------------------------


def read_job(job: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": job["name"],
        "id": job["id"],
        "conclusion": job["conclusion"],
        "runner": job.get("runner_name"),
        "labels": job.get("labels"),
        "created_at": job.get("created_at"),
        "started_at": job["started_at"],
        "completed_at": job["completed_at"],
        "queued_s": seconds(job.get("created_at"), job["started_at"]),
        "s": seconds(job["started_at"], job["completed_at"]),
        "steps": [
            {
                "name": step["name"],
                "conclusion": step["conclusion"],
                "s": seconds(step.get("started_at"), step.get("completed_at")),
            }
            for step in job.get("steps", [])
        ],
    }
    if TOKEN and job["status"] == "completed":
        try:
            log = get(f"{API}/repos/{REPOSITORY}/actions/jobs/{job['id']}/logs")
            entry["log"] = read_log(log.decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as error:
            entry["log"] = {"error": f"HTTP {error.code}"}
    return entry


def made_the_venv(run: dict[str, Any]) -> bool | None:
    """Whether `make_venv.py` ran: the venv was not restored."""
    for job in run["jobs"]:
        for step in job["steps"]:
            if "make_venv.py" in step["name"]:
                return bool(step["conclusion"] not in ("skipped", None))
    return None


def read_branch(branch: str) -> list[dict[str, Any]]:
    listed = api(f"actions/workflows/{WORKFLOW}/runs?branch={branch}&per_page=100")
    runs = []
    for run in sorted(listed["workflow_runs"], key=lambda r: r["created_at"]):
        jobs = api(f"actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
        entry: dict[str, Any] = {
            "id": run["id"],
            "sha": run["head_sha"][:7],
            "message": (run.get("head_commit") or {}).get("message", "").split("\n")[0],
            "event": run["event"],
            "attempt": run.get("run_attempt"),
            "status": run["status"],
            "conclusion": run["conclusion"],
            "created_at": run["created_at"],
            "jobs": [read_job(job) for job in jobs],
        }
        ends = [j["completed_at"] for j in entry["jobs"] if j["completed_at"]]
        entry["push_to_result_s"] = (
            seconds(run["created_at"], max(ends))
            if ends and run["status"] == "completed"
            else None
        )
        entry["made_the_venv"] = made_the_venv(entry)
        runs.append(entry)
    return runs


def describe(branch: str, runs: list[dict[str, Any]]) -> list[str]:
    lines = [f"== {branch}: {len(runs)} runs"]
    for run in runs:
        lines.append(
            f"  run {run['id']} {run['sha']} {run['conclusion']} "
            f"push->result {run['push_to_result_s']} s, "
            f"made the venv: {run['made_the_venv']}  ({run['message'][:60]})"
        )
        for job in run["jobs"]:
            log = job.get("log", {})
            lines.append(
                f"    {job['name']:<9} {job['conclusion']:<9} {job['s']} s "
                f"(queued {job['queued_s']} s)  first red: {log.get('first_red')}  "
                f"{log.get('verdict') or ''}"
            )
            for step in job["steps"]:
                lines.append(
                    f"      {step['s']!s:>6} s  {step['conclusion']:<8} {step['name']}"
                )
            for name, verdict, s in log.get("summary", []):
                lines.append(f"        check.py {name:<9} {verdict:<7} {s} s")
            for key in ("env", "failing", "caches"):
                for line in log.get(key, []):
                    lines.append(f"        {key}: {line}")
            if log.get("vitest"):
                lines.append(f"        vitest: {log['vitest']}")
    warm = [
        r["push_to_result_s"]
        for r in runs
        if r["made_the_venv"] is False and r["push_to_result_s"] is not None
    ]
    if warm:
        lines.append(
            f"  warm runs (venv restored): {warm}, median {statistics.median(warm)} s"
        )
    return lines


def main() -> None:
    branches = sys.argv[1:]
    if not branches:
        sys.exit(__doc__)
    result: dict[str, Any] = (
        json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    )
    result.update(
        {
            "repository": REPOSITORY,
            "workflow": WORKFLOW,
            "read_at": time.strftime("%Y-%m-%d %H:%M"),
            "logs": bool(TOKEN),
        }
    )
    result.setdefault("branches", {})
    for branch in branches:
        runs = read_branch(branch)
        result["branches"][branch] = runs
        print("\n".join(describe(branch, runs)), flush=True)
    OUT.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
