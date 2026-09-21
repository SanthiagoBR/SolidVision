"""API routers package -- one resource per router."""

from .capabilities import router as capabilities_router
from .devices import router as devices_router
from .images import router as images_router
from .jobs import router as jobs_router

__all__ = [
    "capabilities_router",
    "devices_router",
    "images_router",
    "jobs_router",
]
