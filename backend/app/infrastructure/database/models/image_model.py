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

SHA256_HEX_LENGTH = 64

# The projection width of the RFC-023 CLIP checkpoint, and therefore the
# width of the `vector` column the RFC-023 migration created. Named here
# rather than repeated as a literal because search has to validate query
# vectors against the same number, and the two must not be able to drift.
EMBEDDING_DIMENSION = 512


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
    # A module constant rather than `settings.embedding_dimension` because
    # this is physical schema, pinned by a migration: it must not silently
    # follow a runtime env var away from what the database actually holds.
    # `test_image_model_embedding_column_matches_configured_dimension` asserts
    # the two stay in agreement.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIMENSION), nullable=True
    )
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    file_modified_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # A hex-encoded SHA-256 digest is exactly 64 characters, so the length
    # is physical schema rather than a guess. Nullable because RFC-024
    # added the column without backfilling: rows indexed before it read
    # back as NULL, which the skip decision treats as "unknown", never as
    # "matches" (see `plan_indexing`).
    content_hash: Mapped[str | None] = mapped_column(
        String(SHA256_HEX_LENGTH), nullable=True
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
            content_hash=None,
        )

    def to_domain(self) -> Image:
        """Create a domain entity from this ORM model."""
        return Image(
            id=ImageId(self.id),
            path=ImagePath(self.path),
            filename=self.filename,
            extension=self.extension,
        )
