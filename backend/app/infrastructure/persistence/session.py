"""Fábrica de sessões SQLAlchemy e dependência da infraestrutura de persistência."""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy.orm import Session, sessionmaker

from app.infrastructure.logging.logger import get_logger

from .engine import EngineInstance

logger = get_logger(__name__)

SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=EngineInstance,
    autoflush=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    """Fornece uma sessão do banco para futuras dependências do FastAPI."""
    logger.debug("Opening SQLAlchemy session")
    session = SessionLocal()
    try:
        yield session
    finally:
        logger.debug("Closing SQLAlchemy session")
        session.close()
