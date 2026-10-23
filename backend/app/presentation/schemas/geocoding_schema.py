"""The answer of `GET /api/v1/geocode` (RFC-043)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.value_objects.place import Place


class PlaceSchema(BaseModel):
    latitude: float = Field(description="Decimal degrees, WGS 84")
    longitude: float = Field(description="Decimal degrees, WGS 84")
    label: str = Field(
        description="What the service understood the address to be: show it, "
        "so the user can tell whether it found the right one"
    )


class GeocodeResponseSchema(BaseModel):
    place: PlaceSchema | None = Field(
        description="The best match, or null when the address matched nothing"
    )

    @classmethod
    def from_place(cls, place: Place | None) -> GeocodeResponseSchema:
        if place is None:
            return cls(place=None)
        return cls(
            place=PlaceSchema(
                latitude=place.position.latitude,
                longitude=place.position.longitude,
                label=place.label,
            )
        )
