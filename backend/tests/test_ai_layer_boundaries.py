"""The architectural boundary RFC-023 exists to protect.

The whole point of putting CLIP behind `EmbeddingModelPort` is that
replacing it -- with a fine-tuned checkpoint, SigLIP, an ONNX runtime, a
multilingual model -- must not touch a single Domain or Application file.
That property is only real if it is enforced, so this walks the actual
source of both layers and fails on any AI-library import.

RFC-026 added a third walked directory, `app/presentation/api/`, and a
second rule for it: routers may reach for neither an AI library nor a
database driver. `AI_Context.md` listed both as forbidden dependencies
and nothing checked either, which was safe only while Presentation had no
routes in it.

`app/infrastructure/ai/` is deliberately not checked: that package is where
these imports belong. Neither is `app/presentation/dependencies/`, for the
same reason -- see `test_the_composition_root_is_exempt_from_the_router_rules`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
DOMAIN_DIR = BACKEND_DIR / "app" / "domain"
APPLICATION_DIR = BACKEND_DIR / "app" / "application"

# RFC-026 put the first real route in `app/presentation/api/`, which makes
# it the first commit that could break `AI_Context.md`'s two unchecked
# dependency rules -- Presentation -> Database and Presentation -> AI
# Models. Both had been trivially satisfied only because Presentation was
# empty. Note what is *not* in this path: `app/presentation/dependencies/`
# must import a session factory and the CLIP adapter, because composing
# Infrastructure is exactly what a composition root is for. The walk
# covers the routers, not the wiring.
PRESENTATION_API_DIR = BACKEND_DIR / "app" / "presentation" / "api"

CHECKED_LAYERS = [DOMAIN_DIR, APPLICATION_DIR, PRESENTATION_API_DIR]

FORBIDDEN_ROOTS = ("torch", "transformers", "langdetect", "PIL", "numpy")
FORBIDDEN_NAMES = ("clip", "siglip", "huggingface")
PERSISTENCE_ROOTS = ("sqlalchemy", "psycopg", "pgvector", "alembic")


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


@pytest.mark.parametrize("layer", CHECKED_LAYERS, ids=lambda p: p.name)
def test_the_layer_actually_contains_source_to_check(layer: Path) -> None:
    """Guards the guard: a wrong path would make every check below vacuous."""
    assert layer.is_dir(), f"{layer} does not exist"
    assert _python_files(layer), f"no Python files found under {layer}"


@pytest.mark.parametrize("layer", CHECKED_LAYERS, ids=lambda p: p.name)
def test_layer_does_not_import_ai_libraries(layer: Path) -> None:
    for path in _python_files(layer):
        for module in _imported_modules(path):
            root = module.split(".")[0]
            assert root not in FORBIDDEN_ROOTS, (
                f"{path.relative_to(BACKEND_DIR)} imports {module!r}; AI libraries "
                f"belong in app/infrastructure/ai/, behind EmbeddingModelPort"
            )


@pytest.mark.parametrize("layer", CHECKED_LAYERS, ids=lambda p: p.name)
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


def test_the_batch_capability_did_not_leak_implementation_vocabulary() -> None:
    """RFC-024 section 16: batching must not teach Domain about tensors.

    `test_the_embedding_port_stays_model_agnostic` already scans the whole
    file, but these are the words a batch API specifically invites -- the
    ones you reach for when explaining *why* several images at once is
    faster -- so they get named explicitly rather than left to a list
    written before the method existed.
    """
    port_source = (DOMAIN_DIR / "services" / "embedding_model_port.py").read_text(
        encoding="utf-8"
    )

    for term in ("tensor", "cuda", "gpu", "device", "pixel", "dtype", "forward pass"):
        assert term not in port_source.lower(), f"{term!r} leaked into the port"


def test_the_embedding_port_exposes_a_batch_capability() -> None:
    """Guards the guard above: the checks are vacuous if the method is gone."""
    from app.domain.services.embedding_model_port import EmbeddingModelPort

    assert hasattr(EmbeddingModelPort, "encode_images")
    assert "encode_images" not in EmbeddingModelPort.__abstractmethods__


def test_the_content_hasher_port_names_no_implementation() -> None:
    """The same contract discipline, applied to RFC-024's second port."""
    port_source = (DOMAIN_DIR / "services" / "content_hasher_port.py").read_text(
        encoding="utf-8"
    )

    for term in ("hashlib", "sha256", "pathlib", "open(", "read("):
        assert term not in port_source.lower(), f"{term!r} leaked into the port"


def test_the_application_layer_depends_only_on_ports_for_hashing() -> None:
    """Hashing is filesystem I/O, so the concrete hasher stays below.

    An `import ... Sha256ContentHasher` in a use case would compile and
    pass every behavioural test in the suite -- this is the only thing
    that would notice.
    """
    for path in _python_files(APPLICATION_DIR):
        for module in _imported_modules(path):
            assert "sha256" not in module.lower(), (
                f"{path.relative_to(BACKEND_DIR)} imports {module!r}; the hashing "
                f"implementation belongs behind ContentHasherPort"
            )


def test_the_presentation_routers_never_reach_for_the_database() -> None:
    """`AI_Context.md`: Presentation -> Database is a forbidden dependency.

    The temptation this removes is concrete rather than theoretical. A
    route that imported `SessionLocal` to open its own session, or
    `ImageModel` to filter one more column, would work, would pass every
    behavioural test RFC-026 wrote, and would have dismantled the
    separation the three previous RFCs spent their whole scope building.
    """
    for path in _python_files(PRESENTATION_API_DIR):
        for module in _imported_modules(path):
            root = module.split(".")[0]
            assert root not in PERSISTENCE_ROOTS, (
                f"{path.relative_to(BACKEND_DIR)} imports {module!r}; persistence "
                f"belongs behind ImageRepository, composed in "
                f"app/presentation/dependencies/"
            )
            assert not module.endswith(("persistence.session", "persistence.engine")), (
                f"{path.relative_to(BACKEND_DIR)} imports {module!r}; a route that "
                f"opens its own session also has to remember to close it"
            )


def test_the_composition_root_is_exempt_from_the_router_rules() -> None:
    """Guards the guard: the exemption has to be real, not accidental.

    If `dependencies/` ever moved under `api/`, the walk above would start
    failing on the one module that is *supposed* to import SQLAlchemy and
    CLIP, and the tempting fix would be to weaken the rule for everyone.
    """
    composition_root = BACKEND_DIR / "app" / "presentation" / "dependencies"

    assert composition_root.is_dir()
    assert PRESENTATION_API_DIR not in composition_root.parents

    modules = _imported_modules(composition_root / "__init__.py")
    assert any("clip" in module.lower() for module in modules)
    assert any(module.split(".")[0] == "sqlalchemy" for module in modules)


def test_use_cases_never_reach_for_the_database_directly() -> None:
    """`AI_Context.md`: everything through `ImageRepository`.

    RFC-024 added a bulk metadata read, which is exactly the kind of
    "just one query" that tempts a use case to open a session itself.
    """
    for path in _python_files(APPLICATION_DIR):
        for module in _imported_modules(path):
            root = module.split(".")[0]
            assert root not in ("sqlalchemy", "psycopg", "pgvector", "alembic"), (
                f"{path.relative_to(BACKEND_DIR)} imports {module!r}; persistence "
                f"belongs behind ImageRepository"
            )
