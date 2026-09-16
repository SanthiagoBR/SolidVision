"""Indexing-job identifier for the domain layer (RFC-029)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from uuid import UUID

from app.domain.exceptions import InvalidJobIdentifierError


@dataclass(frozen=True)
class JobId:
    """Strongly typed identifier for one indexing job.

    Modelled on `DeviceId` and `ImageId` so that a bare `uuid.UUID` cannot
    be passed where a job was meant. That is the whole of its job: three
    id-shaped tables now exist, and every repository method below takes
    one of them.

    **Allocated with `uuid4`, not derived.** `ImageId` and `DeviceId` are
    `uuid5` over something durable, because the same file and the same
    disk have to land on the same row every time they are seen. A job is
    the opposite kind of thing: it is an *event*, and asking to index the
    same folder twice is two jobs rather than one row written twice
    (RFC-029 section 5.1).
    """

    value: UUID

    def __init__(self, value: UUID | str) -> None:
        object.__setattr__(self, "value", self._normalize(value))

    @staticmethod
    def new() -> JobId:
        """Mint an identifier for a job that is being created right now."""
        return JobId(uuid.uuid4())

    @staticmethod
    def _normalize(value: UUID | str) -> UUID:
        if isinstance(value, UUID):
            return value

        if isinstance(value, str):
            if not value.strip():
                raise InvalidJobIdentifierError("Job identifier cannot be empty")
            try:
                return UUID(value)
            except ValueError as exc:
                raise InvalidJobIdentifierError(
                    "Job identifier must be a valid UUID"
                ) from exc

        raise InvalidJobIdentifierError("Job identifier must be a UUID or string")

    def __str__(self) -> str:
        return str(self.value)
