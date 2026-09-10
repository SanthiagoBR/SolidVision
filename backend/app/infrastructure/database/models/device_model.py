"""SQLAlchemy model for persisting storage devices (RFC-027)."""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Integer, String
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.entities.device import Device
from app.domain.value_objects.device_id import DeviceId, VolumeIdentity, VolumeKind
from app.infrastructure.persistence.base import Base


class DeviceModel(Base):
    """SQLAlchemy representation of a `Device` domain entity.

    **There is no `drive_letter` column and no `mount_point` column**, and
    the absence is the design. A removable volume's letter is assigned by
    mount order, so persisting one would make a stationary file's recorded
    location change on its own -- which is the defect RFC-027 exists to
    remove, not a convenience it forgot.

    **There is no `is_connected` column either.** It would be wrong on
    every read taken after the user unplugged the disk, and nothing would
    ever correct it, because Windows does not notify this process
    (RFC-027 section 7). Connection is resolved by enumerating what is
    mounted at the moment the question is asked.
    """

    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )
    volume_identity: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    """The opaque string the operating system reports for this volume.

    Unique, because two rows claiming the same physical disk is exactly
    the duplication RFC-027 removes. Stored as text and never parsed: it
    is a key, and the only operations on a key are equality and hashing.
    """

    volume_kind: Mapped[str] = mapped_column(String, nullable=False)
    """Which platform's scheme `volume_identity` came from.

    Persisted so that adding the Linux or macOS adapter needs no migration
    (RFC-027 section 4.1), and so that identical-looking strings from two
    platforms stay two devices.
    """

    label: Mapped[str] = mapped_column(String, nullable=False)
    filesystem_label: Mapped[str | None] = mapped_column(String, nullable=True)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    first_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    """When the disk was last observed plugged in -- history, not state.

    Answers "when did I last see it", which stays true forever. It never
    answers "is it plugged in now", which no stored value can keep.
    """

    last_scan_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_scan_file_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """How many supported files the last scan found on this volume.

    The declared denominator of "% indexed" (RFC-027 section 8). Kept
    apart from anything in `images` because the two count different
    things: rows in `images` can only ever describe files somebody already
    scanned, so a percentage computed from them alone reaches 100% after
    any complete run and never sees a file that was never looked at.

    Nullable because a device that has never been scanned has no
    denominator, and a missing denominator must render as "not scanned",
    never as zero.
    """

    __table_args__ = (
        CheckConstraint("total_bytes >= 0", name="total_bytes_non_negative"),
        CheckConstraint(
            "last_scan_file_count >= 0", name="last_scan_file_count_non_negative"
        ),
    )

    @classmethod
    def from_domain(cls, device: Device) -> DeviceModel:
        """Create an ORM model from a domain entity.

        `first_seen_at` and `last_seen_at` are NOT NULL in the schema but
        optional on the entity, so a device constructed without them is
        stamped with "now" here rather than rejected. That is the right
        place for the default: the timestamps describe when persistence
        observed the disk, and the domain entity is usable in tests and in
        memory without inventing a clock.
        """
        seen_at = datetime.datetime.now(tz=datetime.UTC)
        return cls(
            id=device.id.value,
            volume_identity=device.volume_identity.value,
            volume_kind=device.volume_identity.kind.value,
            label=device.label,
            filesystem_label=device.filesystem_label,
            total_bytes=device.total_bytes,
            first_seen_at=device.first_seen_at or seen_at,
            last_seen_at=device.last_seen_at or seen_at,
            last_scan_at=device.last_scan_at,
            last_scan_file_count=device.last_scan_file_count,
        )

    def to_domain(self) -> Device:
        """Create a domain entity from this ORM model."""
        return Device(
            id=DeviceId(self.id),
            volume_identity=VolumeIdentity(
                value=self.volume_identity, kind=VolumeKind(self.volume_kind)
            ),
            label=self.label,
            filesystem_label=self.filesystem_label,
            total_bytes=self.total_bytes,
            first_seen_at=self.first_seen_at,
            last_seen_at=self.last_seen_at,
            last_scan_at=self.last_scan_at,
            last_scan_file_count=self.last_scan_file_count,
        )
