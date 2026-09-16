"""API routers package -- one resource per router."""

from .images import router as images_router
from .jobs import router as jobs_router

__all__ = ["images_router", "jobs_router"]
