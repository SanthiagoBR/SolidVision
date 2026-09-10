"""PostgreSQL-backed implementation of the device repository port (RFC-027)."""

from __future__ import annotations

import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.entities.device import Device
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.value_objects.device_id import DeviceId, VolumeIdentity
from app.infrastructure.database.models.device_model import DeviceModel


class PostgresDeviceRepository(DeviceRepository):
    """Repository implementation backed by PostgreSQL via SQLAlchemy."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, device: Device) -> None:
        """Create the row, or update the one that already carries this id.

        A genuine upsert, because the natural call site is a worker
        starting a run against a disk it has probably seen before: the id
        is derived from the volume identity, so the second run over the
        same disk arrives here with the same primary key and needs the
        row refreshed, not rejected.

        `first_seen_at` is written on insert and never touched again --
        it is the one field that answers a question about the past, and
        an upsert that overwrote it would quietly turn "since March" into
        "since just now" on every run. Everything else moves: the label
        when the user renames the disk, the capacity when it is replaced,
        the scan counters after a scan, and `last_seen_at` always.

        The rollback on failure follows `PostgresImageRepository`: the
        session is shared with the image writes for the rest of the run,
        so a rejected device row must not leave the transaction in a
        pending-rollback state that fails the *next* unrelated write.
        """
        try:
            existing = self._session.get(DeviceModel, device.id.value)
            if existing is None:
                self._session.add(DeviceModel.from_domain(device))
            else:
                self._apply(existing, device)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    @staticmethod
    def _apply(model: DeviceModel, device: Device) -> None:
        """Copy every mutable field of `device` onto an existing row."""
        model.volume_identity = device.volume_identity.value
        model.volume_kind = device.volume_identity.kind.value
        model.label = device.label
        model.filesystem_label = device.filesystem_label
        model.total_bytes = device.total_bytes
        model.last_seen_at = device.last_seen_at or datetime.datetime.now(
            tz=datetime.UTC
        )
        model.last_scan_at = device.last_scan_at
        model.last_scan_file_count = device.last_scan_file_count

    def get(self, device_id: DeviceId) -> Device | None:
        model = self._session.get(DeviceModel, device_id.value)
        return model.to_domain() if model is not None else None

    def get_by_volume_identity(self, identity: VolumeIdentity) -> Device | None:
        """Look the device up by what the operating system just reported.

        Both halves of the identity are in the `WHERE`. The string alone
        is unique in the schema today, but the kind is what stops two
        platforms' identically-shaped identifiers from being read as one
        volume, and leaving it out here would make that depend on a
        constraint rather than on the query.
        """
        statement = select(DeviceModel).where(
            DeviceModel.volume_identity == identity.value,
            DeviceModel.volume_kind == identity.kind.value,
        )
        model = self._session.execute(statement).scalar_one_or_none()
        return model.to_domain() if model is not None else None

    def list(self) -> list[Device]:
        statement = select(DeviceModel)
        models = self._session.execute(statement).scalars().all()
        return [model.to_domain() for model in models]

    def delete(self, device_id: DeviceId) -> None:
        """Delete the row, letting the foreign key refuse while images remain.

        No cascade, and the refusal is the feature. `images.device_id` is
        a plain foreign key with no `ON DELETE`, so PostgreSQL raises
        rather than removing rows whose embeddings cost hours of
        inference -- exactly what RFC-027 section 6.3 says must never
        happen quietly.
        """
        model = self._session.get(DeviceModel, device_id.value)
        if model is None:
            return
        try:
            self._session.delete(model)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
