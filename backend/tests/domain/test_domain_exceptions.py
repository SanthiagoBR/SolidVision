from __future__ import annotations

import app.domain.exceptions as domain_exceptions
from app.domain.exceptions import (
    DomainError,
    ImageAlreadyExistsError,
    ImageNotFoundError,
    InvalidImageIdentifierError,
    InvalidImagePathError,
    UnsupportedImageExtensionError,
)


def test_all_domain_exceptions_inherit_from_domain_error() -> None:
    assert issubclass(ImageNotFoundError, DomainError)
    assert issubclass(ImageAlreadyExistsError, DomainError)
    assert issubclass(InvalidImageIdentifierError, DomainError)
    assert issubclass(InvalidImagePathError, DomainError)
    assert issubclass(UnsupportedImageExtensionError, DomainError)


def test_domain_error_inherits_from_exception() -> None:
    assert issubclass(DomainError, Exception)


def test_default_messages_are_assigned() -> None:
    assert str(ImageNotFoundError()) == "Image not found."
    assert str(ImageAlreadyExistsError()) == "Image already exists."
    assert str(InvalidImageIdentifierError()) == "Image identifier must be a valid UUID"
    assert str(InvalidImagePathError()) == "Invalid image path."
    assert str(UnsupportedImageExtensionError()) == "Unsupported image extension."


def test_custom_messages_override_defaults() -> None:
    assert str(ImageNotFoundError("custom")) == "custom"
    assert str(InvalidImagePathError("custom path")) == "custom path"


def test_all_exceptions_are_exported_via_package_init() -> None:
    expected = {
        "DomainError",
        "ImageAlreadyExistsError",
        "ImageNotFoundError",
        "InvalidEmbeddingVectorError",
        "InvalidImageIdentifierError",
        "InvalidImagePathError",
        "UnsupportedImageExtensionError",
    }
    assert set(domain_exceptions.__all__) == expected
