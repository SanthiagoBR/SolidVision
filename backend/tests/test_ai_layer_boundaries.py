"""The architectural boundary RFC-023 exists to protect.

The whole point of putting CLIP behind `EmbeddingModelPort` is that
replacing it -- with a fine-tuned checkpoint, SigLIP, an ONNX runtime, a
multilingual model -- must not touch a single Domain or Application file.
That property is only real if it is enforced, so this walks the actual
source of both layers and fails on any AI-library import.

`app/infrastructure/ai/` is deliberately not checked: that package is where
these imports belong.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
DOMAIN_DIR = BACKEND_DIR / "app" / "domain"
APPLICATION_DIR = BACKEND_DIR / "app" / "application"

FORBIDDEN_ROOTS = ("torch", "transformers", "langdetect", "PIL", "numpy")
FORBIDDEN_NAMES = ("clip", "siglip", "huggingface")


def _python_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*.py"))


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    return modules


@pytest.mark.parametrize("layer", [DOMAIN_DIR, APPLICATION_DIR], ids=lambda p: p.name)
def test_the_layer_actually_contains_source_to_check(layer: Path) -> None:
    """Guards the guard: a wrong path would make every check below vacuous."""
    assert layer.is_dir(), f"{layer} does not exist"
    assert _python_files(layer), f"no Python files found under {layer}"


@pytest.mark.parametrize("layer", [DOMAIN_DIR, APPLICATION_DIR], ids=lambda p: p.name)
def test_layer_does_not_import_ai_libraries(layer: Path) -> None:
    for path in _python_files(layer):
        for module in _imported_modules(path):
            root = module.split(".")[0]
            assert root not in FORBIDDEN_ROOTS, (
                f"{path.relative_to(BACKEND_DIR)} imports {module!r}; AI libraries "
                f"belong in app/infrastructure/ai/, behind EmbeddingModelPort"
            )


@pytest.mark.parametrize("layer", [DOMAIN_DIR, APPLICATION_DIR], ids=lambda p: p.name)
def test_layer_never_names_a_specific_model(layer: Path) -> None:
    """No `clip`/`siglip` in an import path above Infrastructure."""
    for path in _python_files(layer):
        for module in _imported_modules(path):
            lowered = module.lower()
            for name in FORBIDDEN_NAMES:
                assert name not in lowered, (
                    f"{path.relative_to(BACKEND_DIR)} imports {module!r}; the model "
                    f"choice must not be visible above Infrastructure"
                )


def test_the_embedding_port_stays_model_agnostic() -> None:
    """The port's own text must not leak the current implementation.

    A future RFC swapping CLIP out should not have to rewrite this file, so
    nothing about checkpoints, tensors, tokenizers, or translation may
    appear in it (RFC-023 section 2).
    """
    port_source = (DOMAIN_DIR / "services" / "embedding_model_port.py").read_text(
        encoding="utf-8"
    )

    for term in (
        "clip",
        "siglip",
        "torch",
        "transformers",
        "hugging",
        "tokenizer",
        "processor",
        "checkpoint",
        "translat",
    ):
        assert term not in port_source.lower(), f"{term!r} leaked into the port"


def test_fake_embedding_model_remains_available_as_a_test_double() -> None:
    """RFC-023 replaces the production wiring, not the fake (section 18)."""
    from app.domain.services.embedding_model_port import EmbeddingModelPort
    from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel

    assert issubclass(FakeEmbeddingModel, EmbeddingModelPort)


def test_importing_the_fake_does_not_drag_in_torch() -> None:
    """`app.infrastructure.ai.__init__` must stay free of the heavy imports.

    Most of the fast suite imports `FakeEmbeddingModel`; re-exporting the
    CLIP adapter from the package `__init__` would make every one of those
    imports load torch and transformers for nothing.
    """
    ai_package_source = (
        BACKEND_DIR / "app" / "infrastructure" / "ai" / "__init__.py"
    ).read_text(encoding="utf-8")

    assert "ClipEmbeddingModel" not in ai_package_source.split('"""')[-1]
