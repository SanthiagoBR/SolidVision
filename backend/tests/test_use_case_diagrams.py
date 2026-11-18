"""The use case diagrams are drawn from one model, and cover every requirement.

`scripts/use_case_diagrams.py` draws the SVGs of docs/casos-de-uso and the
tables of docs/casos-de-uso.md from its model of actors, use cases and
relationships. What is tested here is that what the repository carries is
what the model gives -- the drift test, as `THIRD-PARTY-NOTICES.md` has one --
and that the model answers for every functional requirement of
docs/requisitos.md.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts" / "use_case_diagrams.py"


@pytest.fixture(scope="module")
def diagrams() -> ModuleType:
    """The script, loaded from its file: `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location("use_case_diagrams", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_diagrams_and_the_tables_are_what_the_model_gives(
    diagrams: ModuleType,
) -> None:
    assert (
        diagrams.main(["--check"]) == 0
    ), "run python scripts/use_case_diagrams.py and commit what it writes"


def test_every_functional_requirement_has_a_use_case(diagrams: ModuleType) -> None:
    text = (REPOSITORY / "docs" / "requisitos.md").read_text(encoding="utf-8")
    requirements = diagrams.read_requirements(text)

    assert len(requirements) == 72
    assert diagrams.model_problems(requirements) == []


def test_a_requirement_without_a_use_case_is_reported(diagrams: ModuleType) -> None:
    requirements = diagrams.read_requirements(
        "### 3.1 Dispositivos\n\n| RF-99 | Um requisito novo. | — | 📋 |\n"
    )

    problems = diagrams.model_problems(requirements)

    assert "RF-99 has no use case" in problems
    assert any("RF-01 is not in" in problem for problem in problems)


def test_a_github_anchor_keeps_the_accents_and_drops_the_dots(
    diagrams: ModuleType,
) -> None:
    anchor = diagrams.anchor

    assert anchor("3.1 Dispositivos") == "31-dispositivos"
    assert (
        anchor("3.7 Abrir, acompanhar e fechar o app")
        == "37-abrir-acompanhar-e-fechar-o-app"
    )
    assert anchor("3.2 Indexação") == "32-indexação"
