"""The CI's workflow, read as text: `.github/workflows/ci.yml` (RFC-049 6).

Four things, each on one line of the file, so no YAML parser: every action
pinned by its commit, the Python that `check.py` runs on, only names that
`check.py` knows, and a token that only reads. Each check returns what it
found wrong, and runs on the real file and on a copy with its defect planted.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"


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


# --------------------------------------------------------------------------
# The four checks
# --------------------------------------------------------------------------


USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<target>.*?)\s*$")
PINNED = re.compile(
    r"(?P<action>actions/[\w.-]+)(?:/[\w.-]+)?"
    r"@(?P<commit>[0-9a-f]{40})\s+#\s+v\d+\.\d+\.\d+"
)
"""An action of the `actions` organization, by a 40-character commit, with
its tag in a comment (section 4.5). `cache/restore` and `cache/save` are
the action `actions/cache`."""


def unpinned_actions(text: str) -> list[str]:
    problems = []
    commits: dict[str, set[str]] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        uses = USES.match(line)
        if uses is None:
            continue
        pinned = PINNED.fullmatch(uses["target"])
        if pinned is None:
            problems.append(f"line {number}: {uses['target']}")
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


VERSION = re.compile(
    r"^\s*(?P<key>PYTHON_VERSION|python-version):\s*(?P<value>[^#]*?)\s*(?:#.*)?$"
)
FROM_THE_JOB = "${{ env.PYTHON_VERSION }}"


def python_problems(text: str, expected: str) -> list[str]:
    """The job's `PYTHON_VERSION` is `check.py`'s, and `setup-python` reads
    it rather than a version of its own (section 4.3)."""
    problems = []
    declared = 0
    for number, line in enumerate(text.splitlines(), start=1):
        version = VERSION.match(line)
        if version is None:
            continue
        value = version["value"].strip("'\"")
        if version["key"] == "PYTHON_VERSION":
            declared += 1
            if value != expected:
                problems.append(f"line {number}: {value}, check.py says {expected}")
        elif value != FROM_THE_JOB:
            problems.append(f"line {number}: python-version is {value}")
    if not declared:
        problems.append("no PYTHON_VERSION")
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


# --------------------------------------------------------------------------
# The real file
# --------------------------------------------------------------------------


def test_every_action_is_pinned_by_its_commit(workflow: str) -> None:
    assert unpinned_actions(workflow) == []


def test_the_python_is_the_one_check_py_runs_on(
    workflow: str, check: ModuleType
) -> None:
    assert python_problems(workflow, check.PYTHON_VERSION) == []


def test_check_py_is_called_only_with_names_it_knows(
    workflow: str, check: ModuleType
) -> None:
    assert unknown_check_names(workflow, check.choose) == []


def test_the_token_only_reads(workflow: str) -> None:
    assert write_permissions(workflow) == []


# --------------------------------------------------------------------------
# Each check against its defect (section 9)
# --------------------------------------------------------------------------


def plant(text: str, old: str, new: str) -> str:
    """The workflow with one defect: the text it replaces must be there."""
    assert old in text, old
    return text.replace(old, new, 1)


def first_commit(text: str) -> str:
    found = re.search(r"@([0-9a-f]{40})", text)
    assert found is not None
    return found.group(1)


def test_an_action_by_its_tag_is_refused(workflow: str) -> None:
    commit = first_commit(workflow)
    planted = plant(workflow, f"@{commit}  # v7.0.1", "@v7")

    problems = unpinned_actions(planted)

    assert len(problems) == 1
    assert problems[0].endswith(": actions/checkout@v7")


def test_an_action_outside_the_actions_organization_is_refused(workflow: str) -> None:
    planted = plant(workflow, "uses: actions/checkout@", "uses: tj-actions/checkout@")

    assert len(unpinned_actions(planted)) == 1


def test_an_action_at_two_commits_is_refused(workflow: str) -> None:
    """`cache/restore` and `cache/save` are one action, at one commit."""
    commit = re.search(r"actions/cache/save@([0-9a-f]{40})", workflow)
    assert commit is not None
    planted = plant(workflow, commit.group(1), "0" * 40)

    assert unpinned_actions(planted) == ["actions/cache at 2 commits"]


def test_another_python_is_refused(workflow: str, check: ModuleType) -> None:
    expected = check.PYTHON_VERSION
    planted = plant(workflow, f'"{expected}"', '"3.12.10"')

    problems = python_problems(planted, expected)

    assert len(problems) == 1
    assert problems[0].endswith(f": 3.12.10, check.py says {expected}")


def test_a_python_version_of_its_own_is_refused(
    workflow: str, check: ModuleType
) -> None:
    planted = plant(workflow, FROM_THE_JOB, '"3.12.9"')

    assert len(python_problems(planted, check.PYTHON_VERSION)) == 1


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
