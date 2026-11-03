"""The sweep of RFC-046 section 5.5, with the repository in memory.

One test per row of the table of section 4 that the sweep decides, the date a
mark keeps, and the comparison by parts that keeps `2018` from protecting
`2018b`. The rows of the table the job runner decides -- the disk unplugged,
the disk in a drawer -- are in `test_job_runner_sweep.py`.
"""

from __future__ import annotations

import datetime
import uuid

from app.application.use_cases.sweep_missing_images import (
    SweepMissingImagesUseCase,
    SweepReport,
)
from app.domain.entities.image import Image
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.image_presence import MissingImages
from app.domain.value_objects.indexing_record import IndexingRecord
from app.domain.value_objects.job_scope import JobScope
from app.infrastructure.filesystem.image_identity import compute_image_id
from tests.application.fakes import FakeImageRepository
from tests.conftest import TEST_DEVICE_ID

SCAN = datetime.datetime(2026, 10, 31, 9, 0, tzinfo=datetime.UTC)
LATER = SCAN + datetime.timedelta(days=7)


class Clock:
    def __init__(self, now: datetime.datetime = SCAN) -> None:
        self.now = now

    def __call__(self) -> datetime.datetime:
        return self.now


def image(path: str) -> Image:
    relative = ImagePath(path)
    return Image(
        id=compute_image_id(TEST_DEVICE_ID, relative),
        device_id=TEST_DEVICE_ID,
        relative_path=relative,
        filename=relative.value.stem,
        extension="jpg",
    )


def indexed(repository: FakeImageRepository, *paths: str) -> list[Image]:
    pictures = [image(path) for path in paths]
    for picture in pictures:
        repository.save_indexed(
            IndexingRecord(
                image=picture,
                embedding=EmbeddingVector([0.1, 0.2]),
                file_size=1024,
                file_modified_at=SCAN,
                content_hash="a" * 64,
            )
        )
    return pictures


def ids(*paths: str) -> set[ImageId]:
    return {image(path).id for path in paths}


def missing_paths(repository: FakeImageRepository) -> set[str]:
    return {
        row.relative_path
        for row in repository.list_presence(TEST_DEVICE_ID)
        if row.missing
    }


def sweep(
    repository: FakeImageRepository,
    listed: set[ImageId],
    unreadable: tuple[str, ...] = (),
    clock: Clock | None = None,
) -> SweepReport:
    return SweepMissingImagesUseCase(repository, clock or Clock()).execute(
        TEST_DEVICE_ID, listed, tuple(JobScope(folder) for folder in unreadable)
    )


class TestTheTableOfSection4:
    def test_a_photo_deleted_for_good_is_marked(self) -> None:
        repository = FakeImageRepository()
        indexed(repository, "a/kept.jpg", "a/deleted.jpg")

        report = sweep(repository, ids("a/kept.jpg"))

        assert missing_paths(repository) == {"a/deleted.jpg"}
        assert (report.marked, report.unmarked, report.protected) == (1, 0, 0)

    def test_a_photo_deleted_by_the_explorer_is_marked_with_its_recycled_copy(
        self,
    ) -> None:
        """The bin is no longer listed (section 5.6): its old row goes too."""
        repository = FakeImageRepository()
        indexed(
            repository,
            "a/kept.jpg",
            "a/deleted.jpg",
            "$RECYCLE.BIN/S-1-5-21-1/$RAB12CD.jpg",
        )

        report = sweep(repository, ids("a/kept.jpg"))

        assert missing_paths(repository) == {
            "a/deleted.jpg",
            "$RECYCLE.BIN/S-1-5-21-1/$RAB12CD.jpg",
        }
        assert report.marked == 2

    def test_a_moved_folder_leaves_its_old_rows_marked(self) -> None:
        repository = FakeImageRepository()
        indexed(repository, "old/1.jpg", "old/2.jpg")
        # The job indexed the new path before the sweep, as a job does.
        indexed(repository, "new/1.jpg", "new/2.jpg")

        report = sweep(repository, ids("new/1.jpg", "new/2.jpg"))

        assert missing_paths(repository) == {"old/1.jpg", "old/2.jpg"}
        assert report.marked == 2
        assert repository.count_by_device() == {TEST_DEVICE_ID: 2}

    def test_the_folder_moved_back_swaps_the_marks(self) -> None:
        repository = FakeImageRepository()
        indexed(repository, "old/1.jpg", "new/1.jpg")
        sweep(repository, ids("new/1.jpg"))

        report = sweep(repository, ids("old/1.jpg"), clock=Clock(LATER))

        assert missing_paths(repository) == {"new/1.jpg"}
        assert (report.marked, report.unmarked) == (1, 1)

    def test_an_unreadable_folder_keeps_its_rows_visible(self) -> None:
        repository = FakeImageRepository()
        indexed(repository, "ok/a.jpg", "bloqueada/b.jpg", "bloqueada/sub/c.jpg")

        report = sweep(repository, ids("ok/a.jpg"), unreadable=("bloqueada",))

        assert missing_paths(repository) == set()
        assert report.protected == 2
        assert report.protected_by_folder == ((JobScope("bloqueada"), 2),)
        assert repository.mark_presence_calls == []

    def test_a_removed_extension_marks_its_photos(self) -> None:
        """Not in the listing, so not found: the card's sentence is the cost."""
        repository = FakeImageRepository()
        indexed(repository, "a/x.jpg", "a/y.png")

        sweep(repository, ids("a/x.jpg"))

        assert missing_paths(repository) == {"a/y.png"}


class TestTheMarks:
    def test_a_row_already_marked_keeps_its_date(self) -> None:
        repository = FakeImageRepository()
        indexed(repository, "a/kept.jpg", "a/deleted.jpg")
        sweep(repository, ids("a/kept.jpg"), clock=Clock(SCAN))

        report = sweep(repository, ids("a/kept.jpg"), clock=Clock(LATER))

        assert report.marked == 0
        assert repository.count_missing_by_device() == {
            TEST_DEVICE_ID: MissingImages(count=1, since=SCAN)
        }

    def test_nothing_to_do_writes_nothing(self) -> None:
        repository = FakeImageRepository()
        indexed(repository, "a/kept.jpg")

        report = sweep(repository, ids("a/kept.jpg"))

        assert report == SweepReport(marked=0, unmarked=0, protected=0)
        assert repository.mark_presence_calls == []

    def test_marking_and_unmarking_are_one_write(self) -> None:
        repository = FakeImageRepository()
        indexed(repository, "old/1.jpg", "new/1.jpg")
        sweep(repository, ids("new/1.jpg"))
        repository.mark_presence_calls.clear()

        sweep(repository, ids("old/1.jpg"), clock=Clock(LATER))

        assert repository.mark_presence_calls == [
            (frozenset(ids("new/1.jpg")), frozenset(ids("old/1.jpg")), LATER)
        ]

    def test_the_rows_are_read_once_at_the_end(self) -> None:
        repository = FakeImageRepository()
        indexed(repository, "a/kept.jpg")

        sweep(repository, ids("a/kept.jpg"))

        assert repository.list_presence_calls == [TEST_DEVICE_ID]

    def test_a_marked_row_inside_an_unreadable_folder_stays_marked(self) -> None:
        repository = FakeImageRepository()
        indexed(repository, "pasta/x.jpg")
        sweep(repository, set())

        report = sweep(repository, set(), unreadable=("pasta",), clock=Clock(LATER))

        assert missing_paths(repository) == {"pasta/x.jpg"}
        assert (report.marked, report.unmarked, report.protected) == (0, 0, 0)

    def test_another_devices_rows_are_never_touched(self) -> None:
        from tests.application.fakes import make_device

        other = make_device("\\\\?\\Volume{0000abcd-0000-0000-0000-000000000000}\\")
        repository = FakeImageRepository()
        theirs = Image(
            id=ImageId(uuid.uuid4()),
            device_id=other.id,
            relative_path=ImagePath("a/theirs.jpg"),
            filename="theirs",
            extension="jpg",
        )
        repository.save_indexed(
            IndexingRecord(
                image=theirs,
                embedding=EmbeddingVector([0.1, 0.2]),
                file_size=1,
                file_modified_at=SCAN,
                content_hash="a" * 64,
            )
        )

        sweep(repository, set())

        assert repository.count_missing_by_device() == {}


class TestInsideComparesParts:
    def test_2018_does_not_protect_2018b(self) -> None:
        repository = FakeImageRepository()
        indexed(repository, "2018/x.jpg", "2018b/y.jpg")

        report = sweep(repository, set(), unreadable=("2018",))

        assert missing_paths(repository) == {"2018b/y.jpg"}
        assert report.protected_by_folder == ((JobScope("2018"), 1),)

    def test_a_nested_folder_is_inside(self) -> None:
        repository = FakeImageRepository()
        indexed(repository, "2018/junho/festa/x.jpg")

        report = sweep(repository, set(), unreadable=("2018/junho",))

        assert missing_paths(repository) == set()
        assert report.protected == 1

    def test_an_unreadable_root_protects_everything(self) -> None:
        repository = FakeImageRepository()
        indexed(repository, "a/x.jpg", "y.jpg")

        report = sweep(repository, set(), unreadable=(".",))

        assert missing_paths(repository) == set()
        assert report.protected == 2
