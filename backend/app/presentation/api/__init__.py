"""API presentation package."""

from fastapi import FastAPI

from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger
from app.presentation.routes import health_router

logger = get_logger(__name__)

app = FastAPI(
    title=settings.project_name,
    description="Infrastructure health API for SolidVision",
    version=settings.project_version,
)

logger.info("FastAPI application initialized")
app.include_router(health_router)

__all__ = ["app"]
