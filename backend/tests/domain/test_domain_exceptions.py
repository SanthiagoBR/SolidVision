from __future__ import annotations

import app.domain.exceptions as domain_exceptions
from app.domain.exceptions import (
    ConflictError,
    DeviceBusyError,
    DeviceNotConnectedError,
    DeviceNotFoundError,
    DomainError,
    EmbeddingDimensionMismatchError,
    EmptySearchQueryError,
    FileGoneError,
    GoneError,
    IllegalJobTransitionError,
    ImageAlreadyExistsError,
    ImageNotFoundError,
    InvalidCaptureDateError,
    InvalidDateRangeError,
    InvalidDeviceIdentifierError,
    InvalidImageIdentifierError,
    InvalidImagePathError,
    InvalidJobIdentifierError,
    InvalidJobScopeError,
    InvalidSearchLimitError,
    InvalidVolumeIdentityError,
    JobNotFoundError,
    NotFoundError,
    ThumbnailNotFoundError,
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
    assert issubclass(InvalidCaptureDateError, DomainError)
    assert issubclass(InvalidDateRangeError, DomainError)
    assert issubclass(JobNotFoundError, DomainError)
    assert issubclass(DeviceBusyError, DomainError)
    assert issubclass(IllegalJobTransitionError, DomainError)
    assert issubclass(InvalidJobScopeError, DomainError)
    assert issubclass(InvalidJobIdentifierError, DomainError)
    assert issubclass(FileGoneError, DomainError)
    assert issubclass(ThumbnailNotFoundError, DomainError)


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
    assert str(InvalidCaptureDateError()) == "Invalid capture date."
    assert str(InvalidDateRangeError()) == "Invalid date range."
    assert str(JobNotFoundError()) == "Indexing job not found."
    assert str(DeviceBusyError()) == "Device already has an active job."
    assert str(IllegalJobTransitionError()) == "Illegal indexing job transition."
    assert str(InvalidJobScopeError()) == "Invalid indexing job scope."
    assert str(FileGoneError()) == "File is no longer on its device."
    assert str(ThumbnailNotFoundError()) == "Thumbnail not found."


def test_custom_messages_override_defaults() -> None:
    assert str(ImageNotFoundError("custom")) == "custom"
    assert str(InvalidImagePathError("custom path")) == "custom path"


def test_all_exceptions_are_exported_via_package_init() -> None:
    expected = {
        "ConflictError",
        "DeviceBusyError",
        "DeviceNotConnectedError",
        "DeviceNotFoundError",
        "DomainError",
        "EmbeddingDimensionMismatchError",
        "EmptySearchQueryError",
        "FileGoneError",
        "GoneError",
        "IllegalJobTransitionError",
        "ImageAlreadyExistsError",
        "ImageNotFoundError",
        "InvalidCaptureDateError",
        "InvalidDateRangeError",
        "InvalidDeviceIdentifierError",
        "InvalidEmbeddingVectorError",
        "InvalidImageIdentifierError",
        "InvalidImagePathError",
        "InvalidJobIdentifierError",
        "InvalidJobScopeError",
        "InvalidSearchLimitError",
        "InvalidVolumeIdentityError",
        "JobNotFoundError",
        "NotFoundError",
        "ThumbnailNotFoundError",
        "UnsupportedImageExtensionError",
    }
    assert set(domain_exceptions.__all__) == expected


def test_the_two_status_bearing_bases_sit_between_domain_error_and_the_leaves() -> None:
    """RFC-029 gave `DomainError` two intermediate bases (section 7.1).

    They exist so that Presentation can answer 404 and 409 without a table
    of exception classes, and the hierarchy is what carries that -- a new
    "not found" error gets its status by inheriting, not by anyone
    remembering to register it.
    """
    assert issubclass(NotFoundError, DomainError)
    assert issubclass(ConflictError, DomainError)
    assert not issubclass(NotFoundError, ConflictError)
    assert not issubclass(ConflictError, NotFoundError)

    assert issubclass(JobNotFoundError, NotFoundError)
    assert issubclass(DeviceNotFoundError, NotFoundError)
    assert issubclass(DeviceBusyError, ConflictError)
    assert issubclass(IllegalJobTransitionError, ConflictError)
    assert issubclass(DeviceNotConnectedError, ConflictError)


def test_a_malformed_request_stays_a_plain_domain_error() -> None:
    """The 400 cases, stated as what they are *not*.

    An invalid scope would not start working if the disk were plugged in
    or the queue emptied, which is exactly what separates it from the
    conflict cases above.
    """
    for error in (InvalidJobScopeError, InvalidJobIdentifierError):
        assert issubclass(error, DomainError)
        assert not issubclass(error, NotFoundError)
        assert not issubclass(error, ConflictError)


def test_rfc_030_rebased_image_not_found_and_added_a_gone_base() -> None:
    """`ImageNotFoundError` moved onto `NotFoundError`; `GoneError` is new.

    The re-basing is safe because nothing raised `ImageNotFoundError`
    before `GET /api/v1/images/{id}` existed (RFC-030 section 4.2), and
    it is the move RFC-029 already made for `DeviceNotFoundError`.

    `GoneError` sits beside the other two bases, never under them: a file
    deleted from a connected disk is neither "never existed" nor "try
    again later", and a hierarchy that nested it under either would let
    `status_for` answer with the wrong one.
    """
    assert issubclass(ImageNotFoundError, NotFoundError)
    assert issubclass(ThumbnailNotFoundError, NotFoundError)

    assert issubclass(GoneError, DomainError)
    assert not issubclass(GoneError, NotFoundError)
    assert not issubclass(GoneError, ConflictError)
    assert issubclass(FileGoneError, GoneError)

    # The disconnected-disk case of `/reveal` reuses the RFC-027 error
    # rather than a second class for the same fact.
    assert issubclass(DeviceNotConnectedError, ConflictError)
    assert not issubclass(DeviceNotConnectedError, GoneError)
