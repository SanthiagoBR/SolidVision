"""Resolving where indexed images are, per request (RFC-030 sections 4.1 and 4.3).

Against `FakeDeviceLocator`, which counts calls. The count is the point of
the second class below: the real locator enumerates every mounted volume
on each call and must not cache, so the only way to keep a page of results
cheap is to ask once per disk inside the request -- and a test that did not
count would pass just as happily with one enumeration per hit.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.application.use_cases.resolve_image_location import (
    ResolveImageLocationUseCase,
)
from app.domain.entities.device import Device
from app.domain.entities.image import Image
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from tests.application.fakes import FakeDeviceLocator, FakeDeviceRepository, make_device

HD2 = make_device(
    volume_value="\\\\?\\Volume{00000000-0000-0000-0000-0000000000a2}\\", label="HD2"
)
HD3 = make_device(
    volume_value="\\\\?\\Volume{00000000-0000-0000-0000-0000000000a3}\\", label="HD3"
)


def image_on(device: Device, relative: str) -> Image:
    return Image(
        id=ImageId(uuid.uuid4()),
        device_id=device.id,
        relative_path=ImagePath(relative),
        filename=Path(relative).stem,
        extension=Path(relative).suffix.lstrip("."),
    )


def resolver(
    locator: FakeDeviceLocator, devices: tuple[Device, ...] = (HD2, HD3)
) -> ResolveImageLocationUseCase:
    return ResolveImageLocationUseCase(
        device_repository=FakeDeviceRepository(list(devices)),
        device_locator=locator,
    )


class TestWhatTheAnswerSays:
    def test_a_mounted_device_gives_an_absolute_path_under_its_mount_point(
        self, tmp_path: Path
    ) -> None:
        image = image_on(HD2, "fotos/2018/junho/DJI_0042.JPG")

        located = resolver(FakeDeviceLocator({HD2.id: tmp_path})).execute_one(image)

        assert located.connected
        assert located.device == HD2
        assert located.image.absolute_path == ImagePath(
            tmp_path / "fotos/2018/junho/DJI_0042.JPG"
        )
        assert located.image.relative_path == image.relative_path

    def test_an_unplugged_device_is_an_answer_not_an_error(self) -> None:
        """RFC-030 section 4.1: "it is on HD3" is the result, not a failure."""
        image = image_on(HD3, "fotos/2018/junho/DJI_0042.JPG")

        located = resolver(FakeDeviceLocator()).execute_one(image)

        assert not located.connected
        assert located.image.absolute_path is None
        assert located.device.label == "HD3"
        assert str(located.image.relative_path) == "fotos/2018/junho/DJI_0042.JPG"

    def test_a_stale_path_on_the_entity_is_cleared_when_the_disk_is_gone(
        self, tmp_path: Path
    ) -> None:
        """An `Image` built while the disk was plugged in must not stay "connected"."""
        image = image_on(HD2, "a.jpg")
        stale = Image(
            id=image.id,
            device_id=image.device_id,
            relative_path=image.relative_path,
            filename=image.filename,
            extension=image.extension,
            absolute_path=ImagePath(tmp_path / "a.jpg"),
        )

        located = resolver(FakeDeviceLocator()).execute_one(stale)

        assert located.image.absolute_path is None
        assert not located.connected

    def test_order_and_length_are_kept_exactly(self, tmp_path: Path) -> None:
        """The caller pairs results with a ranking; nothing here may reorder it."""
        images = [
            image_on(HD3, "z.jpg"),
            image_on(HD2, "a.jpg"),
            image_on(HD3, "m.jpg"),
            image_on(HD2, "b.jpg"),
        ]

        located = resolver(FakeDeviceLocator({HD2.id: tmp_path})).execute(images)

        assert [item.image.id for item in located] == [image.id for image in images]
        assert [item.connected for item in located] == [False, True, False, True]

    def test_an_empty_page_asks_nothing(self) -> None:
        locator = FakeDeviceLocator()

        assert resolver(locator).execute([]) == []
        assert locator.mount_point_calls == []

    def test_an_image_whose_device_has_no_row_is_a_bug_not_a_refusal(self) -> None:
        """A foreign key makes this impossible in PostgreSQL; a 500 is right."""
        with pytest.raises(LookupError):
            resolver(FakeDeviceLocator(), devices=(HD2,)).execute(
                [image_on(HD3, "a.jpg")]
            )


class TestTheOperatingSystemIsAskedOncePerDisk:
    """RFC-030 section 4.2, counted."""

    def test_ten_hits_on_two_disks_enumerate_twice(self, tmp_path: Path) -> None:
        locator = FakeDeviceLocator({HD2.id: tmp_path})
        images = [image_on(HD2 if n % 2 else HD3, f"{n}.jpg") for n in range(10)]

        resolver(locator).execute(images)

        assert len(locator.mount_point_calls) == 2
        assert set(locator.mount_point_calls) == {HD2.id, HD3.id}

    def test_nothing_is_remembered_between_calls(self, tmp_path: Path) -> None:
        """A disk unplugged between two requests is seen as unplugged.

        The per-request grouping must not turn into a cache: that is the
        bug RFC-027 section 7 exists to prevent.
        """
        locator = FakeDeviceLocator({HD2.id: tmp_path})
        use_case = resolver(locator)
        image = image_on(HD2, "a.jpg")

        assert use_case.execute_one(image).connected
        locator.disconnect(HD2.id)
        assert not use_case.execute_one(image).connected
        assert len(locator.mount_point_calls) == 2
