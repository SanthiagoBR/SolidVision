"""Health check router for the infrastructure API."""

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

router = APIRouter()
logger = get_logger(__name__)


@router.get("/health", status_code=status.HTTP_200_OK, response_model=None)
def health_check(db: Session = Depends(get_db)) -> dict[str, Any] | JSONResponse:
    """Return the application health status based on database connectivity."""
    logger.info("Health check requested")

    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.warning("Database health check failed")
        return cast(
            dict[str, Any] | JSONResponse,
            JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "unhealthy", "database": "disconnected"},
            ),
        )

    logger.info("Health check succeeded")
    return {
        "status": "healthy",
        "database": "connected",
        "version": settings.project_version,
        "environment": settings.environment,
    }
