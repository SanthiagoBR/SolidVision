"""SQLAlchemy engine configuration for the persistence infrastructure."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

EngineInstance: Engine = create_engine(
    settings.database_url,
    echo=settings.debug,
    future=True,
    pool_pre_ping=True,
)

logger.info("SQLAlchemy engine created")
