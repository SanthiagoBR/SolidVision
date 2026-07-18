"""Smoke tests for the SQLAlchemy persistence foundation."""

from sqlalchemy.orm import DeclarativeBase

from app.infrastructure.persistence.base import Base
from app.infrastructure.persistence.engine import EngineInstance
from app.infrastructure.persistence.session import SessionLocal


def test_base_is_single_declarative_base() -> None:
    """The shared declarative base should be the project's base class."""
    assert issubclass(Base, DeclarativeBase)


def test_engine_and_session_factory_are_available() -> None:
    """The engine and session factory should be initialized for future use."""
    assert EngineInstance is not None
    assert SessionLocal is not None


def test_metadata_naming_convention_is_configured() -> None:
    """The shared metadata should expose the expected naming convention."""
    metadata = Base.metadata
    assert metadata.naming_convention is not None
    assert metadata.naming_convention["pk"] == "pk_%(table_name)s"
    assert metadata.naming_convention["fk"] == (
        "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
    )


def test_engine_has_expected_dialect_for_smoke_test() -> None:
    """The engine should expose a dialect object from SQLAlchemy."""
    assert EngineInstance.dialect is not None
    assert EngineInstance.dialect.name == "postgresql"
