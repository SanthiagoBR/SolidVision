"""Repository interfaces package."""

from app.domain.repositories.device_repository import DeviceRepository
from app.domain.repositories.image_repository import ImageRepository

__all__ = ["DeviceRepository", "ImageRepository"]
