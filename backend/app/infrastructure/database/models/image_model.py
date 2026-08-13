"""SQLAlchemy model for persisting image metadata."""

from __future__ import annotations

import datetime
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, DateTime, String, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.entities.image import Image
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.persistence.base import Base


class ImageModel(Base):
    """SQLAlchemy representation of an image domain entity."""

    __tablename__ = "images"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )
    path: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    extension: Mapped[str] = mapped_column(String, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1152), nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    file_modified_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (UniqueConstraint("path", name="uq_images_path"),)

    @classmethod
    def from_domain(cls, image: Image) -> ImageModel:
        """Create an ORM model from a domain entity.

        The Domain `Image` entity has no embedding or filesystem metadata
        fields, so those persistence-only columns are left unset here; they
        are populated separately by the indexing pipeline once it exists.
        """
        return cls(
            id=image.id.value,
            path=str(image.path),
            filename=image.filename,
            extension=image.extension,
            embedding=None,
            file_size=None,
            file_modified_at=None,
        )

    def to_domain(self) -> Image:
        """Create a domain entity from this ORM model."""
        return Image(
            id=ImageId(self.id),
            path=ImagePath(self.path),
            filename=self.filename,
            extension=self.extension,
        )
