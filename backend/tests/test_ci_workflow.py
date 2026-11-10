"""The CI, read from its files: `.github/workflows/ci.yml` (RFC-049 6), each
`.github/actions/*/action.yml` it calls, and `.github/rulesets/ci.json`, the
ruleset that requires its jobs (RFC-050 7).

The YAML is read as text, each thing on one line of a file, so no YAML
parser: every action pinned by its commit, the Python that `check.py` runs
on, only names that `check.py` knows, a token that only reads, and the
workflow's jobs. The ruleset is JSON, read by the standard library. Each
check returns what it found wrong, and runs on the real files and on a copy
with its defect planted.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
ACTIONS = REPO / ".github" / "actions"
RULESET = REPO / ".github" / "rulesets" / "ci.json"

JOBS = ("frontend", "estatico", "backend")
"""The three checks the ruleset requires by name (RFC-050 section 6)."""
GITHUB_ACTIONS_APP = 15368


def load(name: str) -> ModuleType:
    """A script, loaded from its file: `scripts/` is not a package."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def check() -> ModuleType:
    return load("check")


@pytest.fixture(scope="module")
def workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def actions() -> dict[str, str]:
    """Each local action's `action.yml`, by `<folder>/action.yml`."""
    return {
        f"{path.parent.name}/action.yml": path.read_text(encoding="utf-8")
        for path in sorted(ACTIONS.glob("*/action.yml"))
    }


@pytest.fixture(scope="module")
def ruleset() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(RULESET.read_text(encoding="utf-8"))
    return loaded


def lines(text: str) -> list[tuple[int, str, str | None]]:
    """Each line with its number and the top-level key it falls under."""
    found = []
    section = None
    for number, line in enumerate(text.splitlines(), start=1):
        top = re.match(r"^([\w-]+):", line)
        if top:
            section = top.group(1)
        found.append((number, line, section))
    return found


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------


USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<target>.*?)\s*$")
PINNED = re.compile(
    r"(?P<action>actions/[\w.-]+)(?:/[\w.-]+)?"
    r"@(?P<commit>[0-9a-f]{40})\s+#\s+v\d+\.\d+\.\d+"
)
"""An action of the `actions` organization, by a 40-character commit, with
its tag in a comment (RFC-049 section 4.5). `cache/restore` and `cache/save`
are the action `actions/cache`."""
LOCAL = re.compile(r"\./\.github/actions/(?P<name>[\w.-]+)")
"""A local action (RFC-050 section 7): accepted when its folder has an
`action.yml`, which is read with the workflow."""


def unpinned_actions(files: Mapping[str, str], local: Path = ACTIONS) -> list[str]:
    """Every `uses:` of every file, and each action at one commit across all
    of them."""
    problems = []
    commits: dict[str, set[str]] = {}
    for name, text in files.items():
        for number, line, _ in lines(text):
            uses = USES.match(line)
            if uses is None:
                continue
            target = uses["target"]
            here = LOCAL.fullmatch(target)
            if here is not None:
                if not (local / here["name"] / "action.yml").is_file():
                    problems.append(f"{name} line {number}: {target}, no such action")
                continue
            pinned = PINNED.fullmatch(target)
            if pinned is None:
                problems.append(f"{name} line {number}: {target}")
                continue
            commits.setdefault(pinned["action"], set()).add(pinned["commit"])
    if not commits and not problems:
        problems.append("no `uses:` at all")
    problems += [
        f"{action} at {len(found)} commits"
        for action, found in commits.items()
        if len(found) > 1
    ]
    return problems


SETTING = re.compile(
    r"^\s*(?P<key>PYTHON_VERSION|python-version|PYTHON_NUGET_SHA256|nuget-sha256):"
    r"\s*(?P<value>[^#]*?)\s*(?:#.*)?$"
)
DECLARED = ("PYTHON_VERSION", "PYTHON_NUGET_SHA256")
FROM_THE_WORKFLOW = {
    "python-version": "${{ env.PYTHON_VERSION }}",
    "nuget-sha256": "${{ env.PYTHON_NUGET_SHA256 }}",
}
SHA256 = re.compile(r"[0-9a-fA-F]{64}")


def python_problems(
    workflow: str, actions: Mapping[str, str], expected: str
) -> list[str]:
    """The workflow's `env` says `check.py`'s Python and the SHA-256 of its
    nuget.org package, once each; every input that passes them on reads that
    `env`; and an action has no version of its own (RFC-050 section 7)."""
    problems = []
    declared: Counter[str] = Counter()
    for number, line, section in lines(workflow):
        setting = SETTING.match(line)
        if setting is None:
            continue
        key, value = setting["key"], setting["value"].strip("'\"")
        if key in FROM_THE_WORKFLOW:
            if value != FROM_THE_WORKFLOW[key]:
                problems.append(f"ci.yml line {number}: {key} is {value}")
            continue
        declared[key] += 1
        if section != "env":
            problems.append(f"ci.yml line {number}: {key} outside the workflow's env")
        if key == "PYTHON_VERSION" and value != expected:
            problems.append(f"ci.yml line {number}: {value}, check.py says {expected}")
        if key == "PYTHON_NUGET_SHA256" and SHA256.fullmatch(value) is None:
            problems.append(
                f"ci.yml line {number}: {value} is not 64 hexadecimal characters"
            )
    for key in DECLARED:
        if declared[key] != 1:
            problems.append(f"{key} declared {declared[key]} times")
    for name, text in actions.items():
        for number, line, _ in lines(text):
            setting = SETTING.match(line)
            if setting is None:
                continue
            key, value = setting["key"], setting["value"]
            if key in DECLARED or value not in ("", f"${{{{ inputs.{key} }}}}"):
                problems.append(f"{name} line {number}: {key} of its own")
    return problems


CALL = re.compile(r"scripts/check\.py(?P<arguments>(?:[ \t]+[^\s#]+)*)")


def unknown_check_names(text: str, choose: Callable[[list[str]], object]) -> list[str]:
    """Every `scripts/check.py` call names steps and groups `choose()` takes."""
    problems = []
    calls = 0
    for number, line in enumerate(text.splitlines(), start=1):
        for call in CALL.finditer(line):
            calls += 1
            names = [a for a in call["arguments"].split() if not a.startswith("-")]
            try:
                choose(names)
            except Exception as error:
                problems.append(f"line {number}: {error}")
    if not calls:
        problems.append("no call of scripts/check.py")
    return problems


READ_ONLY = re.compile(r"^permissions:[ \t]*\n[ \t]+contents:[ \t]*read[ \t]*$", re.M)


def write_permissions(text: str) -> list[str]:
    problems = []
    if READ_ONLY.search(text) is None:
        problems.append("no top-level `permissions:` with `contents: read`")
    problems += [
        f"line {number}: {line.strip()}"
        for number, line in enumerate(text.splitlines(), start=1)
        if ": write" in line
    ]
    return problems


JOB = re.compile(r"^  (?P<name>[\w-]+):\s*(?:#.*)?$")


def workflow_jobs(text: str) -> list[str]:
    """The job ids under `jobs:`: the names their checks get."""
    return [
        job["name"]
        for _, line, section in lines(text)
        if section == "jobs" and (job := JOB.match(line)) is not None
    ]


def ruleset_problems(jobs: list[str], ruleset: Mapping[str, Any]) -> list[str]:
    """The ruleset requires exactly the workflow's jobs, from GitHub Actions,
    on the default branch, and nobody bypasses it (RFC-050 section 6)."""
    problems = []
    if (ruleset.get("target"), ruleset.get("enforcement")) != ("branch", "active"):
        problems.append("not an active branch ruleset")
    branches = ruleset.get("conditions", {}).get("ref_name", {}).get("include")
    if branches != ["~DEFAULT_BRANCH"]:
        problems.append(f"on {branches}, not on the default branch")
    if ruleset.get("bypass_actors"):
        problems.append(f"bypassed by {ruleset['bypass_actors']}")
    rules = ruleset.get("rules", [])
    if [rule.get("type") for rule in rules] != ["required_status_checks"]:
        problems.append(f"rules {[rule.get('type') for rule in rules]}")
    required = [
        check
        for rule in rules
        if rule.get("type") == "required_status_checks"
        for check in rule.get("parameters", {}).get("required_status_checks", [])
    ]
    contexts = [check.get("context") for check in required]
    if sorted(map(str, contexts)) != sorted(jobs):
        problems.append(f"requires {contexts}, the workflow's jobs are {jobs}")
    problems += [
        f"{check.get('context')} from app {check.get('integration_id')}"
        for check in required
        if check.get("integration_id") != GITHUB_ACTIONS_APP
    ]
    return problems


# --------------------------------------------------------------------------
# The real files
# --------------------------------------------------------------------------


def test_every_action_is_pinned_by_its_commit(
    workflow: str, actions: dict[str, str]
) -> None:
    assert unpinned_actions({"ci.yml": workflow, **actions}) == []


def test_the_python_is_the_one_check_py_runs_on(
    workflow: str, actions: dict[str, str], check: ModuleType
) -> None:
    assert python_problems(workflow, actions, check.PYTHON_VERSION) == []


def test_check_py_is_called_only_with_names_it_knows(
    workflow: str, check: ModuleType
) -> None:
    assert unknown_check_names(workflow, check.choose) == []


def test_the_token_only_reads(workflow: str) -> None:
    assert write_permissions(workflow) == []


def test_the_jobs_are_the_three_the_ruleset_requires(
    workflow: str, ruleset: dict[str, Any]
) -> None:
    assert workflow_jobs(workflow) == list(JOBS)
    assert ruleset_problems(workflow_jobs(workflow), ruleset) == []


# --------------------------------------------------------------------------
# Each check against its defect (RFC-049 section 9, RFC-050 section 10)
# --------------------------------------------------------------------------


def plant(text: str, old: str, new: str) -> str:
    """The file with one defect: the text it replaces must be there."""
    assert old in text, old
    return text.replace(old, new, 1)


def first_commit(text: str) -> str:
    found = re.search(r"@([0-9a-f]{40})", text)
    assert found is not None
    return found.group(1)


def test_an_action_by_its_tag_is_refused(
    workflow: str, actions: dict[str, str]
) -> None:
    commit = first_commit(workflow)
    planted = plant(workflow, f"@{commit}  # v7.0.1", "@v7")

    problems = unpinned_actions({"ci.yml": planted, **actions})

    assert len(problems) == 1
    assert problems[0].endswith(": actions/checkout@v7")


def test_an_action_by_its_tag_inside_the_local_action_is_refused(
    workflow: str, actions: dict[str, str]
) -> None:
    text = actions["venv/action.yml"]
    planted = plant(
        text, f"cache/restore@{first_commit(text)}  # v6.1.0", "cache/restore@v6"
    )

    problems = unpinned_actions({"ci.yml": workflow, "venv/action.yml": planted})

    assert len(problems) == 1
    assert problems[0].startswith("venv/action.yml line ")
    assert problems[0].endswith(": actions/cache/restore@v6")


def test_an_action_outside_the_actions_organization_is_refused(
    workflow: str, actions: dict[str, str]
) -> None:
    planted = plant(workflow, "uses: actions/checkout@", "uses: tj-actions/checkout@")

    assert len(unpinned_actions({"ci.yml": planted, **actions})) == 1


def test_an_action_at_two_commits_is_refused(
    workflow: str, actions: dict[str, str]
) -> None:
    """`cache/restore` and `cache/save` are one action, at one commit, in the
    workflow and in the local action together."""
    commit = re.search(r"actions/cache/save@([0-9a-f]{40})", workflow)
    assert commit is not None
    planted = plant(workflow, commit.group(1), "0" * 40)

    assert unpinned_actions({"ci.yml": planted, **actions}) == [
        "actions/cache at 2 commits"
    ]


def test_a_local_action_that_is_not_there_is_refused(
    workflow: str, actions: dict[str, str]
) -> None:
    planted = plant(
        workflow, "uses: ./.github/actions/venv", "uses: ./.github/actions/gone"
    )

    problems = unpinned_actions({"ci.yml": planted, **actions})

    assert len(problems) == 1
    assert problems[0].endswith(": ./.github/actions/gone, no such action")


def test_another_python_is_refused(
    workflow: str, actions: dict[str, str], check: ModuleType
) -> None:
    expected = check.PYTHON_VERSION
    planted = plant(workflow, f'"{expected}"', '"3.12.10"')

    problems = python_problems(planted, actions, expected)

    assert len(problems) == 1
    assert problems[0].endswith(f": 3.12.10, check.py says {expected}")


def test_a_python_version_of_its_own_is_refused(
    workflow: str, actions: dict[str, str], check: ModuleType
) -> None:
    planted = plant(workflow, FROM_THE_WORKFLOW["python-version"], '"3.12.9"')

    problems = python_problems(planted, actions, check.PYTHON_VERSION)

    assert len(problems) == 1
    assert problems[0].endswith(": python-version is 3.12.9")


def test_a_python_version_inside_the_action_is_refused(
    workflow: str, actions: dict[str, str], check: ModuleType
) -> None:
    planted = plant(
        actions["venv/action.yml"],
        "        VERSION: ${{ inputs.python-version }}\n",
        "        VERSION: ${{ inputs.python-version }}\n"
        '        PYTHON_VERSION: "3.12.9"\n',
    )

    problems = python_problems(
        workflow, {"venv/action.yml": planted}, check.PYTHON_VERSION
    )

    assert len(problems) == 1
    assert problems[0].endswith(": PYTHON_VERSION of its own")


def test_a_nuget_sha256_that_is_not_hexadecimal_is_refused(
    workflow: str, actions: dict[str, str], check: ModuleType
) -> None:
    found = re.search(r'PYTHON_NUGET_SHA256: "(?P<sha>[^"]*)"', workflow)
    assert found is not None
    planted = plant(workflow, found["sha"], "g" + found["sha"][1:])

    problems = python_problems(planted, actions, check.PYTHON_VERSION)

    assert len(problems) == 1
    assert problems[0].endswith(" is not 64 hexadecimal characters")


def test_a_group_check_py_does_not_know_is_refused(
    workflow: str, check: ModuleType
) -> None:
    planted = plant(workflow, "scripts/check.py python", "scripts/check.py backend")

    problems = unknown_check_names(planted, check.choose)

    assert len(problems) == 1
    assert "unknown: backend" in problems[0]


def test_a_write_permission_is_refused(workflow: str) -> None:
    planted = plant(workflow, "contents: read", "contents: write")

    problems = write_permissions(planted)

    assert problems[0] == "no top-level `permissions:` with `contents: read`"
    assert [p.split(": ", 1)[1] for p in problems[1:]] == ["contents: write"]


def test_a_job_that_asks_for_more_is_refused(workflow: str) -> None:
    planted = plant(
        workflow,
        "    runs-on: ubuntu-24.04\n",
        "    runs-on: ubuntu-24.04\n    permissions:\n      pull-requests: write\n",
    )

    problems = write_permissions(planted)

    assert [p.split(": ", 1)[1] for p in problems] == ["pull-requests: write"]


def test_a_renamed_job_is_refused(workflow: str, ruleset: dict[str, Any]) -> None:
    """The check of a job renamed without the ruleset would stay pending
    forever (RFC-050 section 2.6)."""
    planted = plant(workflow, "\n  backend:\n", "\n  suite:\n")

    problems = ruleset_problems(workflow_jobs(planted), ruleset)

    assert workflow_jobs(planted) == ["frontend", "estatico", "suite"]
    assert problems == [
        "requires ['frontend', 'estatico', 'backend'], "
        "the workflow's jobs are ['frontend', 'estatico', 'suite']"
    ]


def test_a_check_missing_from_the_ruleset_is_refused(
    workflow: str, ruleset: dict[str, Any]
) -> None:
    planted = copy.deepcopy(ruleset)
    checks = planted["rules"][0]["parameters"]["required_status_checks"]
    checks[:] = [c for c in checks if c["context"] != "backend"]

    problems = ruleset_problems(workflow_jobs(workflow), planted)

    assert len(problems) == 1
    assert problems[0].startswith("requires ['frontend', 'estatico'],")


def test_a_check_from_another_app_is_refused(
    workflow: str, ruleset: dict[str, Any]
) -> None:
    planted = copy.deepcopy(ruleset)
    planted["rules"][0]["parameters"]["required_status_checks"][2]["integration_id"] = 1

    assert ruleset_problems(workflow_jobs(workflow), planted) == ["backend from app 1"]


def test_an_actor_on_the_bypass_list_is_refused(
    workflow: str, ruleset: dict[str, Any]
) -> None:
    planted = copy.deepcopy(ruleset)
    planted["bypass_actors"] = [
        {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
    ]

    problems = ruleset_problems(workflow_jobs(workflow), planted)

    assert len(problems) == 1
    assert problems[0].startswith("bypassed by ")
