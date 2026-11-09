"""Router de health check da API de infraestrutura.

O código de status responde pelo banco (RFC-008), e só por ele. `search` diz
como vai o aquecimento dos modelos (RFC-036 §5.2): `loading`, `ready`,
`unavailable` -- com a linha em `search_detail` -- ou `on_demand`. Um 200 é
"pronto" para o launcher, diga o que disser `search`; quem desenha a caixa de
busca continua perguntando aqui.

**Um 200 não grava nada** (RFC-037 §5.3). A interface consulta esta rota a
cada 2 s enquanto o modelo carrega e a cada 30 s depois; a 2 s, as duas linhas
INFO de cada chamada davam umas 86 mil linhas por dia no `api.log` e, com a
linha de acesso do uvicorn, umas 130 mil no log da janela, aonde chega o
stderr do launcher. Um 503 mantém o seu WARNING: este log existe justamente
para um banco que não responde.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger
from app.infrastructure.persistence.session import get_db
from app.presentation.model_warm_up import model_warm_up

router = APIRouter()
logger = get_logger(__name__)


def _search_fields() -> dict[str, Any]:
    readiness = model_warm_up.readiness()
    return {"search": readiness.state.value, "search_detail": readiness.detail}


@router.get("/health", status_code=status.HTTP_200_OK, response_model=None)
def health_check(db: Session = Depends(get_db)) -> dict[str, Any] | JSONResponse:
    """Devolve o estado de saúde da aplicação, a partir da conexão com o banco."""
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.warning("Database health check failed")
        return cast(
            dict[str, Any] | JSONResponse,
            JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": "unhealthy",
                    "database": "disconnected",
                    **_search_fields(),
                },
            ),
        )

    return {
        "status": "healthy",
        "database": "connected",
        "version": settings.project_version,
        "environment": settings.environment,
        **_search_fields(),
    }
