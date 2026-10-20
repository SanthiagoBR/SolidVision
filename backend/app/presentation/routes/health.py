"""Health check router for the infrastructure API.

The status code answers for the database (RFC-008), and only for it. `search`
says how the warm-up of the models is going (RFC-036 section 5.2): `loading`,
`ready`, `unavailable` -- with the line in `search_detail` -- or `on_demand`.
A 200 is "ready" for the launcher whatever `search` says; whoever draws the
search box keeps asking here.

**A 200 writes nothing** (RFC-037 section 5.3). The interface polls this route,
every 2 s while the model loads and every 30 s after; at 2 s, the two INFO lines
of each call were some 86 thousand lines a day in `api.log`, and, with uvicorn's
access line, some 130 thousand in the window's log, which the launcher's stderr
reaches. A 503 keeps its WARNING: a database that does not answer is what this
log is for.
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
    """Return the application health status based on database connectivity."""
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
