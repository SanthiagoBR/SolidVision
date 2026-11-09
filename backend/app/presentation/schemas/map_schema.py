"""Formato da resposta de `GET /api/v1/images/map` (RFC-032 §7).

O contrato de onde a tela do mapa desenha (`frontend/src/map/`, RFC-040).
Células, e não fotos -- um centro e uma contagem por quadrado --, porque a
pergunta é "onde estão as minhas fotos", e a resposta a ela, para 100.000
fotos, são algumas centenas de quadrados, não 100.000 pontos.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.application.use_cases.aggregate_positions import PositionMap


class MapCellSchema(BaseModel):
    """Um quadrado da grade: onde fica o centro, e quantas fotos há nele."""

    latitude: float = Field(
        description=(
            "Centre of the cell, decimal degrees: the photos' latitude rounded "
            "to precision_applied places"
        )
    )
    longitude: float = Field(
        description=(
            "Centre of the cell, decimal degrees: the photos' longitude rounded "
            "to precision_applied places"
        )
    )
    count: int = Field(description="How many photos fall in the cell; never 0")


class MapResponseSchema(BaseModel):
    """O corpo da resposta a um pedido de mapa.

    `precision_applied` devolve a precisão de fato usada, do jeito que a
    busca devolve `limit` (RFC-026): quando a grade pedida teria mais de
    `MAX_MAP_CELLS` quadrados, a grade foi engrossada, e um cliente que não
    conseguisse ver isso confundiria quadrados maiores com uma coleção mais
    esparsa.
    """

    precision_applied: int = Field(
        description=(
            "Decimal places of a degree the cells were rounded to (3 is about "
            "100 m). Lower than requested when the requested grid had too many "
            "cells, or than the finest grid offered."
        )
    )
    cells: list[MapCellSchema] = Field(
        description=(
            "Every non-empty cell in the area, under the device and date "
            "filters, ordered by latitude then longitude. Only images with an "
            "embedding -- the ones a search could return -- are counted."
        )
    )
    excluded_unknown_position: int = Field(
        description=(
            "How many photos under the same device and date filters have no "
            "position, and so are on no map at all -- the same number a 'near "
            "here' search reports. Not restricted to the area: a photo with no "
            "coordinates is in no area."
        )
    )
    excluded_unknown_date: int | None = Field(
        description=(
            "How many photos under the same device filter have no capture "
            "date, and so were left off the map by the date range -- the same "
            "number a search with that range reports. Null when no date range "
            "was sent. Not restricted to the area, as excluded_unknown_position "
            "is not; a photo with neither a date nor a position is counted here "
            "only."
        )
    )

    @classmethod
    def from_map(cls, answer: PositionMap) -> MapResponseSchema:
        """Monta a resposta do caso de uso para o envio, sem acrescentar nada."""
        return cls(
            precision_applied=answer.precision_applied,
            cells=[
                MapCellSchema(
                    latitude=cell.latitude, longitude=cell.longitude, count=cell.count
                )
                for cell in answer.cells
            ],
            excluded_unknown_position=answer.excluded_unknown_position,
            excluded_unknown_date=answer.excluded_unknown_date,
        )
