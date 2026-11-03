"""What the listing of a whole disk found, handed to the observer (RFC-046)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RootListing:
    """The supported files of a whole root, and the folders that could not be read.

    What `IndexingObserver.listed()` receives since RFC-046 section 5.3, in
    place of the bare count RFC-039 section 4.1 gave it. Three fields:

    * `file_count` -- the denominator of "% indexed", counting **only files**:
      the walk knows a folder from a file without a `stat`, so a folder named
      `x.jpg` no longer counts (RFC-046 section 5.2);
    * `files` -- each of those files relative to the root, as text in the posix
      form `ImagePath` keeps. Text and not `PurePath`, for the memory: a disk
      of 116,396 photos holds this list from the listing to the end of the job
      (RFC-046 sections 5.3 and 8);
    * `unreadable` -- the folders the walk could not read, relative to the
      root, in the same form. Nothing under them was listed, so nothing under
      them may be called missing (section 2).

    **No device and no id.** The provider that builds this knows neither; the
    job runner mints the ids from `files` at the end of the job, with the one
    function that mints them during indexing (section 5.5).

    `file_count` is a field, and always `len(files)`: the two are the same set,
    and a listing whose count and files disagree is refused at construction.
    """

    file_count: int
    files: tuple[str, ...]
    unreadable: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.file_count != len(self.files):
            raise ValueError(
                f"A listing of {len(self.files)} files cannot count {self.file_count}"
            )
