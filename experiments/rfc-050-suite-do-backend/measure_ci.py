"""RFC-050 section 9: the workflow's runs, read from GitHub's API.

    python experiments/rfc-050-suite-do-backend/measure_ci.py <branch>... > measure_ci.log

RFC-049's `measure_ci.py`, extended for the `backend` job. For each branch,
every run of ci.yml on it, oldest first: the run's `created_at`, and the
`started_at` and `completed_at` of each job and of each step, with their
conclusions. "Push to result" is the run's `created_at` to the last job's
`completed_at`.

The logs need a token, even on a public repository (the API answers 403
without one). With `GITHUB_TOKEN` set, each job's log is read too, and from
it, by the time stamp GitHub puts on every line:

- each `##[group]` of the log and its seconds to the next one: the steps
  inside the composite action, which the API reports as one step (the base
  Python from nuget.org, restoring the venv, `make_venv.py`, saving it);
- the `backend` job's phases: `initdb` ("creating the cluster" to "starting
  the server"), the server, the migrations, and the `pytest` step of
  check.py ("==> pytest" to "<== pytest");
- pytest's last line (passed, skipped, deselected) and its `-ra` lines;
- check.py's summary, the `env` step's problems, and what each cache step did;

and the repository's cache entries, with their sizes. Without a token, only
the times. The rules in force on `main` are read without a token
(section 10).

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
        "User-Agent": "solidvision-rfc-050-measure",
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


def api(path: str, *, authorized: bool = True) -> Any:
    return json.loads(get(f"{API}/repos/{REPOSITORY}/{path}", authorized=authorized))


def when(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    stamp = stamp.replace("Z", "+00:00")
    # The log's stamps have seven decimals; fromisoformat takes up to six.
    stamp = re.sub(r"(\.\d{6})\d+", r"\1", stamp)
    return datetime.fromisoformat(stamp)


def seconds(start: str | None, end: str | None) -> float | None:
    begun, ended = when(start), when(end)
    if begun is None or ended is None:
        return None
    return round((ended - begun).total_seconds(), 1)


# --------------------------------------------------------------------------
# What a log says
# --------------------------------------------------------------------------

STAMP = re.compile(r"^﻿?(?P<stamp>\d{4}-\d\d-\d\dT[\d:.]+Z) ?")
COLOR = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
SUMMARY_LINE = re.compile(
    r"^\s+(?P<name>\w+)\s+(?P<verdict>passed|FAILED|not run)\s*(?:(?P<s>[\d.]+) s)?$"
)
VITEST = re.compile(r"^\s*(?P<what>Test Files|Tests)\s+(?P<counts>.+)$")
PYTEST = re.compile(r"^=+ (?P<counts>.*?) in (?P<s>[\d.]+)s(?: \([\d:]+\))? =+$")
SHORT = re.compile(r"^(FAILED|SKIPPED|ERROR|XFAIL|XPASS) ")
CACHE = re.compile(
    r"(Cache restored from key|Cache not found|Cache saved|Cache Size|"
    r"Unable to reserve cache|Failed to save|Path Validation Error|"
    r"Another job may be creating this cache|Cache hit occurred)"
)
PHASES = {
    "initdb": ("creating the cluster", "starting the server"),
    "server": ("starting the server", "ready on "),
    "migrations": ("applying migrations", "embedded postgres: database "),
    "pytest": ("==> pytest", "<== pytest"),
    "run": ("embedded postgres: creating the cluster", "embedded postgres: stopped"),
}
"""Each phase of the `backend` job, from the first line with the first text
to the first line after it with the second."""


def stamped(text: str) -> list[tuple[datetime | None, str]]:
    found = []
    for raw in text.splitlines():
        stamp = STAMP.match(raw)
        line = COLOR.sub("", raw[stamp.end() :] if stamp else raw)
        found.append((when(stamp["stamp"]) if stamp else None, line))
    return found


def groups(lines: list[tuple[datetime | None, str]]) -> list[list[Any]]:
    """Each `##[group]` and its seconds to the next one, or to the log's end."""
    marks = [(at, line[len("##[group]") :]) for at, line in lines if line.startswith("##[group]") and at]
    last = next((at for at, _ in reversed(lines) if at), None)
    found = []
    for index, (at, title) in enumerate(marks):
        end = marks[index + 1][0] if index + 1 < len(marks) else last
        found.append([title[:90], round((end - at).total_seconds(), 1) if end else None])
    return found


def phases(lines: list[tuple[datetime | None, str]]) -> dict[str, float]:
    found = {}
    for name, (first, then) in PHASES.items():
        begun = next((i for i, (at, line) in enumerate(lines) if at and first in line), None)
        if begun is None:
            continue
        ended = next(
            (at for at, line in lines[begun + 1 :] if at and then in line), None
        )
        start = lines[begun][0]
        if ended is not None and start is not None:
            found[name] = round((ended - start).total_seconds(), 1)
    return found


def read_log(text: str) -> dict[str, Any]:
    lines = stamped(text)
    plain = [line for _, line in lines]
    found: dict[str, Any] = {"summary": [], "verdict": None, "env": [], "vitest": {}}
    found["caches"] = [line.strip() for line in plain if CACHE.search(line)][:30]
    found["groups"] = groups(lines)
    found["phases"] = phases(lines)
    found["short"] = [line.strip() for line in plain if SHORT.match(line)][:60]
    in_summary = False
    for line in plain:
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
        pytest = PYTEST.match(line.strip())
        if pytest:
            found["pytest"] = {"counts": pytest["counts"], "s": float(pytest["s"])}
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


def made(run: dict[str, Any], job_name: str, what: str) -> bool | None:
    """Whether a step whose title has `what` ran in the job: a top-level step
    by the API, or a step inside the action by its group in the log."""
    for job in run["jobs"]:
        if job["name"] != job_name:
            continue
        for step in job["steps"]:
            if what in step["name"]:
                return bool(step["conclusion"] not in ("skipped", None))
        found = job.get("log", {}).get("groups")
        if found is not None:
            return any(what in title for title, _ in found)
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
        entry["made_the_venv"] = {
            name: made(entry, name, "make_venv.py") for name in ("estatico", "backend")
        }
        entry["made_the_runtime"] = made(entry, "backend", "vendor_postgres.py")
        runs.append(entry)
    return runs


def warm(run: dict[str, Any]) -> bool:
    return run["made_the_runtime"] is False and not any(run["made_the_venv"].values())


def describe(branch: str, runs: list[dict[str, Any]]) -> list[str]:
    lines = [f"== {branch}: {len(runs)} runs"]
    for run in runs:
        lines.append(
            f"  run {run['id']} {run['sha']} {run['conclusion']} attempt {run['attempt']} "
            f"push->result {run['push_to_result_s']} s, made the venv: "
            f"{run['made_the_venv']}, made the runtime: {run['made_the_runtime']}  "
            f"({run['message'][:60]})"
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
            for title, s in log.get("groups", []):
                lines.append(f"        group {s!s:>6} s  {title}")
            for name, s in log.get("phases", {}).items():
                lines.append(f"        phase {name:<10} {s} s")
            if log.get("pytest"):
                lines.append(f"        pytest: {log['pytest']}")
            for name, verdict, s in log.get("summary", []):
                lines.append(f"        check.py {name:<9} {verdict:<7} {s} s")
            for key in ("env", "short", "failing", "caches"):
                for line in log.get(key, []):
                    lines.append(f"        {key}: {line}")
            if log.get("vitest"):
                lines.append(f"        vitest: {log['vitest']}")
    hot = [r["push_to_result_s"] for r in runs if warm(r) and r["push_to_result_s"]]
    if hot:
        lines.append(
            f"  warm runs (venv and runtime restored): {hot}, "
            f"median {statistics.median(hot)} s"
        )
    return lines


def read_caches() -> list[dict[str, Any]]:
    """The repository's cache entries as they are now (needs the token)."""
    listed = api("actions/caches?per_page=100&sort=created_at&direction=asc")
    return [
        {
            "key": cache["key"],
            "ref": cache["ref"],
            "mb": round(cache["size_in_bytes"] / 2**20, 1),
            "created_at": cache["created_at"],
            "last_accessed_at": cache["last_accessed_at"],
        }
        for cache in listed["actions_caches"]
    ]


def read_rules() -> dict[str, Any]:
    """What GitHub enforces on `main`, read without a token (section 10)."""
    try:
        return {
            "rules_on_main": api("rules/branches/main", authorized=False),
            "rulesets": api("rulesets", authorized=False),
        }
    except urllib.error.HTTPError as error:
        return {"error": f"HTTP {error.code}"}


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
    result["rules"] = read_rules()
    print(f"rules on main: {json.dumps(result['rules'].get('rules_on_main'))}")
    if TOKEN:
        result["caches"] = read_caches()
        for cache in result["caches"]:
            print(f"cache {cache['mb']:>7} MB  {cache['ref']}  {cache['key'][:60]}")
    OUT.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
