"""Database model package for persistence infrastructure."""

from app.infrastructure.database.models.device_model import DeviceModel
from app.infrastructure.database.models.image_model import ImageModel

__all__ = ["DeviceModel", "ImageModel"]
