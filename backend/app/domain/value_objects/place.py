"""An address the geocoder resolved, and what it understood it to be (RFC-043)."""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.value_objects.position import Position


@dataclass(frozen=True)
class Place:
    position: Position
    # Shown beside the point: there are many "Rua das Palmeiras", and only the
    # user can tell whether the one found is theirs.
    label: str
