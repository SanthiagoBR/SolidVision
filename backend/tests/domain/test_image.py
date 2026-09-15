from __future__ import annotations

import datetime
import uuid
from dataclasses import FrozenInstanceError

import pytest

from app.domain.entities.image import Image
from app.domain.exceptions import DeviceNotConnectedError, InvalidCaptureDateError
from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath

DEVICE_ID = DeviceId(uuid.UUID(int=7))


def _image(**overrides: object) -> Image:
    fields: dict[str, object] = {
        "id": ImageId(uuid.uuid4()),
        "device_id": DEVICE_ID,
        "relative_path": ImagePath("images/example.png"),
        "filename": "example",
        "extension": "png",
    }
    fields.update(overrides)
    return Image(**fields)  # type: ignore[arg-type]


def test_image_stores_required_attributes() -> None:
    image_id = ImageId(uuid.uuid4())
    relative_path = ImagePath("images/example.png")

    image = Image(
        id=image_id,
        device_id=DEVICE_ID,
        relative_path=relative_path,
        filename="example",
        extension="png",
    )

    assert image.id == image_id
    assert image.device_id == DEVICE_ID
    assert image.relative_path == relative_path
    assert image.filename == "example"
    assert image.extension == "png"


def test_image_is_immutable() -> None:
    image = _image()

    with pytest.raises(FrozenInstanceError):
        image.filename = "other"  # type: ignore[misc]


def test_image_compares_by_identity() -> None:
    image_id = ImageId(uuid.uuid4())
    first = _image(id=image_id, relative_path=ImagePath("images/one.png"))
    second = _image(
        id=image_id,
        device_id=DeviceId(uuid.UUID(int=99)),
        relative_path=ImagePath("images/two.png"),
        extension="jpg",
    )

    assert first == second
    assert hash(first) == hash(second)


def test_image_has_no_infrastructure_behavior() -> None:
    image = _image()

    assert not hasattr(image, "database")
    assert not hasattr(image, "session")


def test_image_has_no_embedding_field() -> None:
    image = _image()

    assert not hasattr(image, "embedding")


def test_image_has_no_absolute_path_by_default() -> None:
    """RFC-027: an image knows its device, not where that device is mounted.

    The default has to be `None` rather than something derived from
    `relative_path`, because a relative path interpreted as absolute would
    resolve against the process's working directory -- a plausible-looking
    path that reads the wrong file.
    """
    assert _image().absolute_path is None


def test_require_absolute_path_returns_it_when_the_device_is_connected() -> None:
    resolved = ImagePath("D:/photos/images/example.png")

    image = _image(absolute_path=resolved)

    assert image.require_absolute_path() == resolved


def test_require_absolute_path_raises_when_the_device_is_not_connected() -> None:
    """The honest failure for a photo on a disk in a drawer (RFC-027 2.3).

    Not `FileNotFoundError`: "the file is gone" justifies dropping a row
    and "the disk is unplugged" must never be allowed to.
    """
    with pytest.raises(DeviceNotConnectedError):
        _image().require_absolute_path()


def test_the_unconnected_failure_names_the_device_and_the_file() -> None:
    """An operator has twenty disks; the message has to say which one."""
    with pytest.raises(DeviceNotConnectedError) as excinfo:
        _image().require_absolute_path()

    message = str(excinfo.value)
    assert "images/example.png" in message
    assert str(DEVICE_ID) in message


def test_absolute_path_does_not_participate_in_equality() -> None:
    """It is a fact about this instant, and equality is about the file.

    The same image resolved on a machine where the disk is `D:` and on one
    where it is `F:` is one image. Letting the resolved path into equality
    would reintroduce, in memory, exactly the instability RFC-027 removed
    from the schema.
    """
    image_id = ImageId(uuid.uuid4())
    unmounted = _image(id=image_id)
    mounted = _image(id=image_id, absolute_path=ImagePath("F:/images/example.png"))

    assert unmounted == mounted
    assert hash(unmounted) == hash(mounted)


class TestCaptureDate:
    """RFC-028: the capture date is an attribute of the photograph."""

    SHOT = datetime.datetime(2018, 7, 14, 15, 32, 5)

    def test_an_image_is_unexamined_by_default(self) -> None:
        """Every construction site that predates RFC-028 keeps working.

        And keeps meaning "never examined" rather than "examined, no date":
        a default of `UNKNOWN` would tell the scan never to look.
        """
        image = _image()

        assert image.captured_at is None
        assert image.capture_source is None
        assert image.capture_date is None

    def test_an_examined_image_exposes_its_capture_date(self) -> None:
        image = _image(
            captured_at=self.SHOT, capture_source=CaptureSource.EXIF_ORIGINAL
        )

        assert image.capture_date == CaptureDate(self.SHOT, CaptureSource.EXIF_ORIGINAL)

    def test_examined_without_a_date_is_distinct_from_never_examined(self) -> None:
        examined = _image(capture_source=CaptureSource.UNKNOWN)

        assert examined.capture_date == CaptureDate.unknown()
        assert _image().capture_date is None

    def test_a_zone_aware_capture_date_is_rejected(self) -> None:
        with pytest.raises(InvalidCaptureDateError):
            _image(
                captured_at=self.SHOT.replace(tzinfo=datetime.UTC),
                capture_source=CaptureSource.EXIF_ORIGINAL,
            )

    def test_a_date_without_a_source_is_rejected(self) -> None:
        with pytest.raises(InvalidCaptureDateError):
            _image(captured_at=self.SHOT)

    def test_the_capture_date_does_not_participate_in_equality(self) -> None:
        """Equality is by id, and a backfilled date does not make a new image."""
        image_id = ImageId(uuid.uuid4())
        before = _image(id=image_id)
        after = _image(
            id=image_id,
            captured_at=self.SHOT,
            capture_source=CaptureSource.EXIF_ORIGINAL,
        )

        assert before == after
        assert hash(before) == hash(after)
