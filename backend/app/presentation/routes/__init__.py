"""Presentation route package for the FastAPI application."""

from .health import router as health_router

__all__ = ["health_router"]
