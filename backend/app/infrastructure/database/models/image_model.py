"""SQLAlchemy model for persisting image metadata."""

from __future__ import annotations

import datetime
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.entities.image import Image
from app.domain.value_objects.capture_source import CaptureSource
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

    captured_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    """When the photograph was taken, as the camera clock read it (RFC-028).

    **`timezone=False`, two columns below a `file_modified_at` that is
    `timezone=True` -- and the difference is the design, not an
    inconsistency to tidy up.** `file_modified_at` comes from `st_mtime`,
    an absolute instant, so it belongs in `TIMESTAMPTZ`. This column comes
    from EXIF `DateTimeOriginal`, which is a local wall-clock reading with
    no zone. Putting it in `TIMESTAMPTZ` would force a zone to be invented
    -- UTC, or the zone of whichever machine ran the scan -- and either
    one moves a photo taken at 22:00 on 31 December into the following
    year for anyone reading it elsewhere (RFC-028 section 5). "Photos from
    2018" is asked in camera-local time and is answered here with no
    conversion at all.

    The accepted cost: shots from two different zones do not order
    strictly against each other.
    """

    capture_source: Mapped[str | None] = mapped_column(String, nullable=True)
    """Where `captured_at` came from -- a `CaptureSource` value, or NULL.

    Three states, and the first two must never be merged:

    - NULL: the row was **never examined** -- it is older than RFC-028,
      or its disk was scanned with extraction off;
    - `'unknown'`: examined, and the file carries no usable date, so
      `captured_at` is NULL;
    - `'exif_original'` / `'exif_digitized'`: the EXIF tag the date was
      read from.

    The NULL / `'unknown'` split is what keeps re-scanning cheap. A scan
    writes a capture date only for rows that are still NULL, so after the
    first pass an unchanged collection costs no writes at all. If
    "examined, no date" were stored as NULL too, every scan would reread
    and rewrite every dateless file forever -- and dateless files are the
    most numerous kind in exactly the collections RFC-028 worries about.

    A plain `String`, not a PostgreSQL `ENUM`, so that adding a source
    does not need a migration.
    """

    thumbnail_path: Mapped[str | None] = mapped_column(String, nullable=True)
    """Where this image's thumbnail is kept, relative to the thumbnail cache (RFC-030).

    NULL means *no thumbnail*: the row predates RFC-030, or rendering
    failed, or its bytes changed and the new thumbnail has not been made
    yet. `GET /images/{id}/thumbnail` answers 404 for all three and the UI
    shows a placeholder (RFC-030 section 7.2).

    **Relative to `settings.thumbnail_directory`, never absolute** -- the
    argument RFC-027 made for `relative_path`, one directory over. An
    absolute location would bake the cache's current directory into every
    row, so moving the cache would leave each one pointing at nothing.

    **This column is why the cheap primary-key rewrite ends here** (RFC-030
    section 7.3). The value is derived from `images.id`, and so is the
    name of the file on disk; re-deriving the id after this point means
    renaming files as well as updating rows. Recorded as an assumed
    consequence, as RFC-029 section 11 anticipated.
    """

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

        The capture date *is* copied, because since RFC-028 it is a field of
        the entity: an attribute of the photograph rather than a change
        signal for the pipeline.
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
            captured_at=image.captured_at,
            capture_source=(
                image.capture_source.value if image.capture_source else None
            ),
            thumbnail_path=None,
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
            captured_at=self.captured_at,
            capture_source=(
                CaptureSource(self.capture_source) if self.capture_source else None
            ),
        )
