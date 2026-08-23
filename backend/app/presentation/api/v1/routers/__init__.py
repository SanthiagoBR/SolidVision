"""API routers package -- one resource per router."""

from .images import router as images_router

__all__ = ["images_router"]
