"""`GET /api/v1/geocode`: um endereço digitado, achado por um serviço online (RFC-043).

A única rota cuja requisição sai da máquina. Só loopback, para que a API nunca
seja um relay para o serviço em nome de mais ninguém; 403 com a configuração
desligada, e não o 404 do `/reveal` -- o `/capabilities` já diz que está
desligada, então esconder a rota não esconderia nada. O endereço nunca vai para
o log (RFC-026 §16.1).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.application.use_cases.geocode_address import GeocodeAddressUseCase
from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger
from app.presentation.dependencies import get_geocode_address_use_case
from app.presentation.local_file_actions import require_loopback_client
from app.presentation.schemas.geocoding_schema import GeocodeResponseSchema

router = APIRouter(tags=["geocoding"])
logger = get_logger(__name__)


def require_address_search_enabled() -> None:
    if not settings.allow_address_search:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Address search is switched off in this installation.",
        )


@router.get(
    "/geocode",
    status_code=status.HTTP_200_OK,
    response_model=GeocodeResponseSchema,
    summary="Find a typed address: the address is sent to an online service",
    dependencies=[
        Depends(require_address_search_enabled),
        Depends(require_loopback_client),
    ],
    responses={
        400: {"description": "The address is empty"},
        403: {"description": "Address search is off, or the caller is not local"},
        503: {"description": "The service did not answer; the coordinates still do"},
    },
)
def geocode(
    q: str = Query(description="The address, as the user typed it"),
    use_case: GeocodeAddressUseCase = Depends(get_geocode_address_use_case),
) -> GeocodeResponseSchema:
    logger.info("address search requested: length=%d", len(q))
    return GeocodeResponseSchema.from_place(use_case.execute(q))
