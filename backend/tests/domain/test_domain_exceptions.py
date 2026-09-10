from __future__ import annotations

import app.domain.exceptions as domain_exceptions
from app.domain.exceptions import (
    DeviceNotConnectedError,
    DeviceNotFoundError,
    DomainError,
    EmbeddingDimensionMismatchError,
    EmptySearchQueryError,
    ImageAlreadyExistsError,
    ImageNotFoundError,
    InvalidDeviceIdentifierError,
    InvalidImageIdentifierError,
    InvalidImagePathError,
    InvalidSearchLimitError,
    InvalidVolumeIdentityError,
    UnsupportedImageExtensionError,
)


def test_all_domain_exceptions_inherit_from_domain_error() -> None:
    assert issubclass(ImageNotFoundError, DomainError)
    assert issubclass(ImageAlreadyExistsError, DomainError)
    assert issubclass(InvalidImageIdentifierError, DomainError)
    assert issubclass(InvalidImagePathError, DomainError)
    assert issubclass(UnsupportedImageExtensionError, DomainError)
    assert issubclass(EmptySearchQueryError, DomainError)
    assert issubclass(InvalidSearchLimitError, DomainError)
    assert issubclass(EmbeddingDimensionMismatchError, DomainError)
    assert issubclass(DeviceNotFoundError, DomainError)
    assert issubclass(DeviceNotConnectedError, DomainError)
    assert issubclass(InvalidDeviceIdentifierError, DomainError)
    assert issubclass(InvalidVolumeIdentityError, DomainError)


def test_domain_error_inherits_from_exception() -> None:
    assert issubclass(DomainError, Exception)


def test_default_messages_are_assigned() -> None:
    assert str(ImageNotFoundError()) == "Image not found."
    assert str(ImageAlreadyExistsError()) == "Image already exists."
    assert str(InvalidImageIdentifierError()) == "Image identifier must be a valid UUID"
    assert str(InvalidImagePathError()) == "Invalid image path."
    assert str(UnsupportedImageExtensionError()) == "Unsupported image extension."
    assert str(EmptySearchQueryError()) == "Search query cannot be empty."
    assert str(InvalidSearchLimitError()) == "Invalid search limit."
    assert (
        str(EmbeddingDimensionMismatchError())
        == "Embedding dimension does not match the index."
    )
    assert str(DeviceNotFoundError()) == "Device not found."
    assert str(DeviceNotConnectedError()) == "Device is not connected."
    assert (
        str(InvalidDeviceIdentifierError()) == "Device identifier must be a valid UUID"
    )
    assert str(InvalidVolumeIdentityError()) == "Invalid volume identity."


def test_custom_messages_override_defaults() -> None:
    assert str(ImageNotFoundError("custom")) == "custom"
    assert str(InvalidImagePathError("custom path")) == "custom path"


def test_all_exceptions_are_exported_via_package_init() -> None:
    expected = {
        "DeviceNotConnectedError",
        "DeviceNotFoundError",
        "DomainError",
        "EmbeddingDimensionMismatchError",
        "EmptySearchQueryError",
        "ImageAlreadyExistsError",
        "ImageNotFoundError",
        "InvalidDeviceIdentifierError",
        "InvalidEmbeddingVectorError",
        "InvalidImageIdentifierError",
        "InvalidImagePathError",
        "InvalidSearchLimitError",
        "InvalidVolumeIdentityError",
        "UnsupportedImageExtensionError",
    }
    assert set(domain_exceptions.__all__) == expected
