"""PostgreSQL-backed implementation of the image repository port."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.entities.image import Image
from app.domain.exceptions import ImageAlreadyExistsError
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord
from app.infrastructure.database.models.image_model import ImageModel


class PostgresImageRepository(ImageRepository):
    """Repository implementation backed by PostgreSQL via SQLAlchemy."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, image: Image) -> None:
        """Persist an image, translating path collisions into a domain error."""
        model = ImageModel.from_domain(image)
        self._session.add(model)
        try:
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise ImageAlreadyExistsError() from exc

    def get(self, image_id: ImageId) -> Image | None:
        model = self._session.get(ImageModel, image_id.value)
        return model.to_domain() if model is not None else None

    def exists(self, image_id: ImageId) -> bool:
        statement = select(ImageModel.id).where(ImageModel.id == image_id.value)
        return self._session.execute(statement).scalar_one_or_none() is not None

    def delete(self, image_id: ImageId) -> None:
        model = self._session.get(ImageModel, image_id.value)
        if model is not None:
            self._session.delete(model)
            self._session.commit()

    def list(self) -> list[Image]:
        statement = select(ImageModel)
        models = self._session.execute(statement).scalars().all()
        return [model.to_domain() for model in models]

    def save_indexed(self, record: IndexingRecord) -> None:
        """Create or update an image row together with its embedding and metadata.

        Unlike `save()`, this is a genuine upsert and does not translate
        `IntegrityError` into `ImageAlreadyExistsError` -- a constraint
        violation here (e.g. the `file_size` CHECK) is a real error, not a
        duplicate-create signal.
        """
        model = self._session.get(ImageModel, record.image.id.value)
        if model is None:
            model = ImageModel.from_domain(record.image)
            self._session.add(model)
        else:
            model.path = str(record.image.path)
            model.filename = record.image.filename
            model.extension = record.image.extension

        model.embedding = list(record.embedding.values)
        model.file_size = record.file_size
        model.file_modified_at = record.file_modified_at
        self._session.commit()

    def get_index_metadata(self, image_id: ImageId) -> IndexMetadata | None:
        model = self._session.get(ImageModel, image_id.value)
        if model is None:
            return None
        return IndexMetadata(
            file_size=model.file_size,
            file_modified_at=model.file_modified_at,
        )
