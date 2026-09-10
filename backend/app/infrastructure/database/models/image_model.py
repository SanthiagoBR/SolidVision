"""SQLAlchemy model for persisting image metadata."""

from __future__ import annotations

import datetime
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.entities.image import Image
from app.domain.value_objects.device_id import DeviceId
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
    device_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("devices.id", name="fk_images_device_id"),
        nullable=False,
    )
    """Which volume this file lives on.

    The half of the location that does not move. RFC-027 replaced the
    absolute `path` column with this plus `relative_path`, because an
    absolute path on Windows starts with a drive letter and a drive letter
    is assigned by mount order -- so the same untouched file changed its
    recorded location, and therefore its `uuid5` id, whenever the disk
    happened to mount as `F:` instead of `D:` (RFC-027 section 2.1).
    """

    relative_path: Mapped[str] = mapped_column(String, nullable=False)
    """Where the file sits within its device, e.g. `fotos/2018/DJI_0042.JPG`.

    The other half. There is deliberately no absolute-path column beside
    it: keeping both forms would invite one of them to go stale, and the
    absolute one is precisely the one that cannot be kept correct, since
    it changes without anybody writing anything.
    """

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

    __table_args__ = (
        UniqueConstraint(
            "device_id", "relative_path", name="uq_images_device_relative_path"
        ),
    )
    """One row per file per device.

    Replaces the old `UNIQUE (path)`, which looked like it prevented
    duplicates and prevented nothing: `D:/fotos/x.JPG` and
    `F:/fotos/x.JPG` are two different strings, so the same file on the
    same disk indexed under two drive letters satisfied the constraint
    twice over. The pair does constrain what it claims to.
    """

    @classmethod
    def from_domain(cls, image: Image) -> ImageModel:
        """Create an ORM model from a domain entity.

        The Domain `Image` entity has no embedding or filesystem metadata
        fields, so those persistence-only columns are left unset here; they
        are populated separately by the indexing pipeline once it exists.
        """
        return cls(
            id=image.id.value,
            device_id=image.device_id.value,
            relative_path=str(image.relative_path),
            filename=image.filename,
            extension=image.extension,
            embedding=None,
            file_size=None,
            file_modified_at=None,
            content_hash=None,
        )

    def to_domain(self) -> Image:
        """Create a domain entity from this ORM model.

        `absolute_path` is left `None`, and that is not an omission. The
        row records where the file is on its *device*; where that device
        is mounted right now is not in the database, on purpose, and is
        not knowable from a row (RFC-027 section 7). Whoever needs a
        usable path resolves the mount point and fills it in -- the
        indexing worker while it scans, and RFC-030 per request.
        """
        return Image(
            id=ImageId(self.id),
            device_id=DeviceId(self.device_id),
            relative_path=ImagePath(self.relative_path),
            filename=self.filename,
            extension=self.extension,
        )
