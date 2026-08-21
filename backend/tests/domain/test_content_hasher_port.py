from __future__ import annotations

import inspect
from abc import ABC
from pathlib import Path
from typing import get_type_hints

import pytest

from app.domain.entities.image import Image
from app.domain.services.content_hasher_port import ContentHasherPort

DOMAIN_DIR = Path(__file__).resolve().parents[2] / "app" / "domain"


def test_content_hasher_port_is_abstract() -> None:
    assert issubclass(ContentHasherPort, ABC)
    assert inspect.isabstract(ContentHasherPort)


def test_content_hasher_port_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        # Instantiating an ABC is the behaviour under test, so the error
        # mypy reports here is the assertion, not a defect.
        ContentHasherPort()  # type: ignore[abstract]


def test_hash_image_is_the_only_abstract_method() -> None:
    assert ContentHasherPort.__abstractmethods__ == {"hash_image"}


def test_content_hasher_port_uses_domain_types_only() -> None:
    hints = get_type_hints(ContentHasherPort.hash_image)

    assert hints["image"] is Image
    assert hints["return"] is str


def test_the_port_names_no_hashing_algorithm_or_filesystem_detail() -> None:
    """The port is a contract, not an implementation announcement.

    A future RFC swapping SHA-256 for BLAKE3, or hashing something other
    than a local file, must not have to rewrite this contract -- exactly
    the property `test_the_embedding_port_stays_model_agnostic` protects
    for `EmbeddingModelPort`.
    """
    source = (DOMAIN_DIR / "services" / "content_hasher_port.py").read_text(
        encoding="utf-8"
    )

    for term in ("sha256", "sha-256", "blake", "hashlib", "open(", "chunk"):
        assert term not in source.lower(), f"{term!r} leaked into the port"
