"""The sweep at the end of a job of the whole disk (RFC-046 sections 5.4 to 5.6).

Real files on `tmp_path`, the real provider, the real use cases; the image
store in memory. Each condition of section 5.4 has its own test, in which
everything else holds and that one fails -- and nothing is marked. The rows of
section 4's table that need a disk -- a folder moved and moved back, a photo
deleted by the Explorer, a folder that cannot be read, the disk unplugged --
are run as jobs.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import pytest
from tests.application.fakes import (
    FakeDeviceLocator,
    FakeImageRepository,
    make_device,
)
from tests.infrastructure.workers.test_job_runner import World

from app.domain.entities.device import Device
from app.domain.entities.image import Image
from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_presence import Presences
from app.domain.value_objects.root_listing import RootListing
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.infrastructure.workers import job_runner
from app.infrastructure.workers.indexing_worker import listed_identity
from app.infrastructure.workers.job_progress_observer import JobProgressObserver
from app.infrastructure.workers.read_ahead import ReadAheadLimits

LIMITS = ReadAheadLimits(files=4, budget_bytes=10**6, max_file_bytes=10**6)
MODES = [pytest.param(None, id="serial"), pytest.param(LIMITS, id="read-ahead")]
RUNNER_LOG = "app.infrastructure.workers.job_runner"
REAL_SCANDIR = Path._scandir  # type: ignore[attr-defined]


class CountingModel(EmbeddingModelPort):
    """The fake model, counting what it was asked to infer."""

    def __init__(self) -> None:
        self._delegate = FakeEmbeddingModel()
        self.inferred: list[str] = []

    def encode_image(
        self, image: Image, content: bytes | None = None
    ) -> EmbeddingVector:
        self.inferred.append(str(image.relative_path))
        return self._delegate.encode_image(image)

    def encode_text(self, text: str) -> EmbeddingVector:
        return self._delegate.encode_text(text)


class ScriptedLocator(FakeDeviceLocator):
    """A disk whose mount point answers from a script, call by call.

    The calls of a job of the whole disk, in order: `_index()` before the
    scan (1), `_record_scan()` when the listing ends (2), the sweep (3) and
    `_completion_of()` (4). An answer of `...` is the real one.
    """

    def __init__(self, device: Device, disk: Path) -> None:
        super().__init__({device.id: disk})
        self.answers: list[Any] = []

    def mount_point(self, device: Device) -> Path | None:
        real = super().mount_point(device)
        if not self.answers:
            return real
        answer = self.answers.pop(0)
        return real if answer is ... else answer


@pytest.fixture()
def disk(tmp_path: Path) -> Path:
    root = tmp_path / "disk"
    for folder, count in (("2018", 4), ("2019", 3)):
        (root / folder).mkdir(parents=True)
        for index in range(count):
            (root / folder / f"photo_{index:03d}.jpg").write_bytes(
                f"{folder}/{index}".encode()
            )
    return root


def world_of(
    disk: Path, read_ahead: ReadAheadLimits | None = None, **options: Any
) -> World:
    return World(disk, read_ahead=read_ahead, **options)


def run(world: World, *scopes: str) -> IndexingJob | None:
    return world.runner.run_job(world.queue(*scopes).id)


def missing(world: World) -> set[str]:
    return {
        row.relative_path
        for row in world.images.list_presence(world.device.id)
        if row.missing
    }


def indexed_then_deleted(world: World, disk: Path) -> None:
    """A first job indexes the disk; then a photo is deleted from it."""
    first = run(world)
    assert first is not None and first.status is JobStatus.COMPLETED
    (disk / "2018" / "photo_000.jpg").unlink()


# ----------------------------------------------------------------------
# It runs, and marks


@pytest.mark.parametrize("read_ahead", MODES)
def test_a_photo_deleted_from_the_disk_is_marked(
    disk: Path, read_ahead: ReadAheadLimits | None
) -> None:
    world = world_of(disk, read_ahead)
    indexed_then_deleted(world, disk)

    finished = run(world)

    assert finished is not None and finished.status is JobStatus.COMPLETED
    assert missing(world) == {"2018/photo_000.jpg"}
    assert world.images.count_by_device() == {world.device.id: 6}


@pytest.mark.parametrize("read_ahead", MODES)
def test_the_same_marks_with_the_read_ahead_on_and_off(
    disk: Path, tmp_path: Path, read_ahead: ReadAheadLimits | None
) -> None:
    """Section 9: with the read-ahead on and off, the result is the same."""
    world = world_of(disk, read_ahead)
    indexed_then_deleted(world, disk)
    (disk / "2019" / "photo_new.jpg").write_bytes(b"new")

    run(world)

    assert missing(world) == {"2018/photo_000.jpg"}
    assert {
        row.relative_path for row in world.images.list_presence(world.device.id)
    } == {
        f"{folder}/photo_{index:03d}.jpg"
        for folder, count in (("2018", 4), ("2019", 3))
        for index in range(count)
    } | {
        "2019/photo_new.jpg"
    }


def test_the_sweep_logs_its_three_numbers(
    disk: Path, caplog: pytest.LogCaptureFixture
) -> None:
    world = world_of(disk)
    indexed_then_deleted(world, disk)

    with caplog.at_level(logging.INFO, logger=RUNNER_LOG):
        run(world)

    assert any(
        "1 photos marked as not found, 0 found again, 0 kept visible"
        in record.getMessage()
        for record in caplog.records
    )


def test_the_ids_minted_from_the_listing_are_the_ids_indexing_writes(
    tmp_path: Path,
) -> None:
    """Section 9, the identity: one translation from a listed file to an id."""
    root = tmp_path / "disk"
    for relative in (
        "DCIM/100MEDIA/DJI_0001.JPG",
        "a b/Foto de Família.jpeg",
        "B/c/d/x.PNG",
        "loose.jpg",
    ):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(relative.encode())
    world = world_of(root, supported_extensions=(".jpg", ".jpeg", ".png"))
    listings: list[RootListing] = []
    original = JobProgressObserver.listed

    def spy(self: JobProgressObserver, listing: RootListing) -> None:
        listings.append(listing)
        original(self, listing)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(JobProgressObserver, "listed", spy)
        run(world)

    (listing,) = listings
    minted = {listed_identity(world.device.id, path)[1] for path in listing.files}
    written = {record.image.id for record in world.images.save_indexed_calls}
    assert len(written) == 4
    assert minted == written
    assert missing(world) == set()


# ----------------------------------------------------------------------
# Each condition of section 5.4, failing alone


def test_a_job_of_some_folders_marks_nothing(
    disk: Path, caplog: pytest.LogCaptureFixture
) -> None:
    world = world_of(disk)
    indexed_then_deleted(world, disk)

    with caplog.at_level(logging.INFO, logger=RUNNER_LOG):
        finished = run(world, "2018")

    assert finished is not None and finished.status is JobStatus.COMPLETED
    assert missing(world) == set()
    assert any("some folders" in record.getMessage() for record in caplog.records)


def test_a_cancelled_job_marks_nothing(disk: Path) -> None:
    world = world_of(disk)
    indexed_then_deleted(world, disk)
    # Two batches of new photos: the cancellation is seen after the first.
    for index in range(8):
        (disk / "2019" / f"photo_new_{index}.jpg").write_bytes(f"new {index}".encode())
    job = world.queue()

    def cancel() -> None:
        world.jobs.save_if_status(world.reload(job).request_cancel(), JobStatus.RUNNING)

    world.images.on_save_indexed_many = cancel
    finished = world.runner.run_job(job.id)

    assert finished is not None and finished.status is JobStatus.CANCELLED
    assert missing(world) == set()


def test_a_job_that_raised_marks_nothing(
    disk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error in the middle of a folder ends the scan, and the job fails.

    The walk raises it rather than handing it to `on_error`
    (`test_list_tree.py`); here the listing raises it whole.
    """
    world = world_of(disk)
    indexed_then_deleted(world, disk)

    def failing_listing(scope_root: Path) -> Any:
        raise OSError(5, "I/O error in the middle of a folder")

    monkeypatch.setattr(
        "app.infrastructure.filesystem.filesystem_image_provider._list_tree",
        failing_listing,
    )

    finished = run(world)

    assert finished is not None and finished.status is JobStatus.FAILED
    assert missing(world) == set()


def test_a_job_taken_over_marks_nothing(disk: Path) -> None:
    world = world_of(disk)
    indexed_then_deleted(world, disk)
    (disk / "2019" / "photo_new.jpg").write_bytes(b"new")
    job = world.queue()

    def taken_back_by_the_reaper() -> None:
        world.jobs.save_if_status(
            world.reload(job).release(world.clock()), JobStatus.RUNNING
        )

    world.images.on_save_indexed_many = taken_back_by_the_reaper
    finished = world.runner.run_job(job.id)

    assert finished is None
    assert missing(world) == set()


def test_a_disk_back_under_another_letter_marks_nothing(
    disk: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Connected, so the job completes; not where it was listed, so no sweep."""
    elsewhere = tmp_path / "another-letter"
    elsewhere.mkdir()
    locator = ScriptedLocator(make_device(label="HD2"), disk)
    world = world_of(disk, locator=locator)
    indexed_then_deleted(world, disk)
    # Before the scan, when the listing ends, at the sweep, at the end.
    locator.answers = [..., ..., elsewhere, elsewhere]

    with caplog.at_level(logging.INFO, logger=RUNNER_LOG):
        finished = run(world)

    assert finished is not None and finished.status is JobStatus.COMPLETED
    assert missing(world) == set()
    assert any(
        "is now at" in record.getMessage() and "marks no photo" in record.getMessage()
        for record in caplog.records
    )


def test_a_listing_that_was_not_kept_marks_nothing(
    disk: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Elsewhere when the listing ended, back by the end: not the disk's listing."""
    elsewhere = tmp_path / "another-letter"
    elsewhere.mkdir()
    locator = ScriptedLocator(make_device(label="HD2"), disk)
    world = world_of(disk, locator=locator)
    indexed_then_deleted(world, disk)
    locator.answers = [..., elsewhere, ..., ...]

    with caplog.at_level(logging.INFO, logger=RUNNER_LOG):
        finished = run(world)

    assert finished is not None and finished.status is JobStatus.COMPLETED
    assert missing(world) == set()
    assert any("was not kept" in record.getMessage() for record in caplog.records)


def test_a_disk_unplugged_mid_job_fails_it_and_marks_nothing(disk: Path) -> None:
    world = world_of(disk)
    indexed_then_deleted(world, disk)
    (disk / "2019" / "photo_new.jpg").write_bytes(b"new")
    world.images.on_save_indexed_many = lambda: world.locator.disconnect(
        world.device.id
    )

    finished = run(world)

    assert finished is not None and finished.status is JobStatus.FAILED
    assert missing(world) == set()


class _SweepFails(FakeImageRepository):
    def list_presence(self, device_id: Any) -> Presences:
        raise RuntimeError("the connection went away")


def test_a_sweep_that_raises_does_not_fail_the_job(
    disk: Path, caplog: pytest.LogCaptureFixture
) -> None:
    world = world_of(disk, images=_SweepFails())

    with caplog.at_level(logging.ERROR, logger=RUNNER_LOG):
        finished = run(world)

    assert finished is not None and finished.status is JobStatus.COMPLETED
    assert finished.progress.processed_images == 7
    assert any("sweep" in record.getMessage() for record in caplog.records)


# ----------------------------------------------------------------------
# The table of section 4, as jobs


class TestAFolderMoved:
    def test_moved_and_moved_back(self, disk: Path) -> None:
        model = CountingModel()
        world = world_of(disk, embedding_model=model)
        run(world)
        assert len(model.inferred) == 7

        (disk / "2018").rename(disk / "2018-movida")
        run(world)

        assert missing(world) == {f"2018/photo_{i:03d}.jpg" for i in range(4)}
        assert model.inferred[7:] == [
            f"2018-movida/photo_{i:03d}.jpg" for i in range(4)
        ]
        assert world.images.count_by_device() == {world.device.id: 7}

        (disk / "2018-movida").rename(disk / "2018")
        run(world)

        assert missing(world) == {f"2018-movida/photo_{i:03d}.jpg" for i in range(4)}
        assert len(model.inferred) == 11, "moving back inferred something"
        assert world.images.count_by_device() == {world.device.id: 7}


@pytest.mark.parametrize("read_ahead", MODES)
def test_the_recycle_bin_and_the_volume_information_are_not_listed(
    disk: Path, read_ahead: ReadAheadLimits | None
) -> None:
    """Section 5.6, in both spellings of the bin."""
    for relative in (
        "$RECYCLE.BIN/S-1-5-21-1/$RAB12CD.jpg",
        "$RECYCLE.BIN/S-1-5-21-1/$IAB12CD.jpg",
        "System Volume Information/x.jpg",
    ):
        (disk / relative).parent.mkdir(parents=True, exist_ok=True)
        (disk / relative).write_bytes(b"system")
    world = world_of(disk, read_ahead)

    finished = run(world)

    assert finished is not None
    assert finished.progress.processed_images == 7
    assert world.devices.record_scan_calls[-1][1] == 7
    assert not any(
        "RECYCLE" in str(record.image.relative_path)
        or "Volume Information" in str(record.image.relative_path)
        for record in world.images.save_indexed_calls
    )


@pytest.mark.skipif(
    sys.platform != "win32", reason="the case-insensitive comparison is Windows'"
)
def test_the_bin_in_another_case_is_the_same_folder(disk: Path) -> None:
    (disk / "$Recycle.Bin" / "S-1").mkdir(parents=True)
    (disk / "$Recycle.Bin" / "S-1" / "$R1.jpg").write_bytes(b"system")
    world = world_of(disk)

    finished = run(world)

    assert finished is not None and finished.progress.processed_images == 7


def test_rows_already_in_the_bin_are_marked_by_the_first_sweep(disk: Path) -> None:
    """The 1,268 rows of `E:`: no migration knows them, the sweep does."""
    (disk / "$RECYCLE.BIN" / "S-1").mkdir(parents=True)
    (disk / "$RECYCLE.BIN" / "S-1" / "$R1.jpg").write_bytes(b"recycled")
    world = world_of(disk)
    # Indexed the way it was before RFC-046, with the bin not excluded.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(job_runner, "SYSTEM_DIRECTORIES", ())
        run(world)
    assert "$RECYCLE.BIN/S-1/$R1.jpg" in {
        str(record.image.relative_path) for record in world.images.save_indexed_calls
    }

    run(world)

    assert missing(world) == {"$RECYCLE.BIN/S-1/$R1.jpg"}


def test_an_unreadable_folder_marks_nothing_and_says_which(
    disk: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    world = world_of(disk)
    run(world)

    def scandir(self: Path) -> Any:
        if self.name == "2018":
            raise PermissionError(13, "Access is denied", str(self))
        return REAL_SCANDIR(self)

    monkeypatch.setattr(Path, "_scandir", scandir)

    with caplog.at_level(logging.INFO):
        finished = run(world)

    assert finished is not None and finished.status is JobStatus.COMPLETED
    assert missing(world) == set()
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and record.name.endswith("job_runner")
    ]
    assert any(
        "2018" in message and "4 of its photos" in message for message in warnings
    )


def test_a_removed_extension_marks_its_photos(disk: Path) -> None:
    (disk / "2019" / "drawing.png").write_bytes(b"png")
    world = world_of(disk)
    run(world)
    narrower = world_of(disk, images=world.images, supported_extensions=(".jpg",))

    run(narrower)

    assert missing(world) == {"2019/drawing.png"}
