"""Infrastructure filesystem abstraction for discovering candidate image files.

**The order this module produces is a correctness requirement, not a
convenience.** RFC-029 section 10 makes the checkpoint a *position* in the
discovery sequence, so "continue after X" is only meaningful if a second
scan of the same folders visits the same files in the same order. A
`sorted()` removed in the name of performance would pass every other test
in the suite and break resumption silently, in a way that depends on the
filesystem.

**And the order is `Path` order, which is not string order.** On Windows
`sorted()` over paths compares part by part and case-insensitively, so

    sorted(Path)  ->  a/x.jpg, a/Z.jpg, a b/x.jpg, B/y.jpg
    sorted(str)   ->  B/y.jpg, a b/x.jpg, a/Z.jpg, a/x.jpg

and PostgreSQL's collation would give a third answer. A resume written as
`relative_path > :checkpoint`, in SQL or in Python, therefore skips and
repeats arbitrary files -- and passes every test whose fixture names are
lowercase and space-free, which is every test written without knowing
this. `is_after_checkpoint()` below is the one comparison, and it compares
paths with paths.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePath

from app.domain.services.indexing_observer import (
    IndexingObserver,
    NullIndexingObserver,
)
from app.domain.value_objects.job_scope import JobScope
from app.infrastructure.filesystem.discovered_image_file import DiscoveredImageFile
from app.infrastructure.filesystem.exif_capture_date import read_capture_date


def discovery_sort_key(relative_path: PurePath) -> PurePath:
    """Return the value discovery order is established by.

    The path itself. That looks like a no-op and is the point: `PurePath`
    already implements the comparison this module needs -- part by part,
    with the running platform's case rule -- so the ordering key is "a
    path of the platform's flavour", and anything that turns one into text
    before comparing has left the ordering behind.

    It exists as a named function so that the two places that must agree
    -- the sort in `discover()` and the resume test below -- are visibly
    the same decision rather than two independent `sorted()` calls.
    """
    return relative_path


def is_after_checkpoint(relative_path: PurePath, checkpoint: PurePath | None) -> bool:
    """Whether `relative_path` still has to be visited after `checkpoint`.

    `checkpoint` is the last file a run made durable, so the file *equal*
    to it is already done and everything strictly greater is not. `None`
    -- a job that has never checkpointed -- means everything is still to
    do.

    Both operands are paths of the same flavour, which is what makes this
    the same comparison that ordered the scan.
    """
    if checkpoint is None:
        return True
    return discovery_sort_key(relative_path) > discovery_sort_key(checkpoint)


class FilesystemImageProvider:
    """Recursively discovers supported image files under a root directory.

    Knows nothing about repositories, embeddings, PostgreSQL, workers, or
    use cases -- it only reads the filesystem via the standard library and,
    since RFC-028, each file's EXIF header via Pillow. Since RFC-029 it
    also knows how to walk *part* of a root and how to pick up where a
    previous run stopped, which are both properties of a scan rather than
    of a job: nothing here knows that jobs exist.
    """

    def __init__(
        self,
        root: Path,
        supported_extensions: Iterable[str],
        extract_capture_date: bool = True,
        scopes: tuple[JobScope, ...] = (),
        resume_after: PurePath | None = None,
        observer: IndexingObserver | None = None,
        report_every: int = 200,
        excluded_directories: tuple[Path, ...] = (),
    ) -> None:
        """Configure a scan of `root`, or of some folders within it.

        `extract_capture_date` is a constructor argument rather than a
        read of `settings`, keeping this class free of configuration: the
        composition root passes `settings.extract_capture_date` in. It
        defaults to on because RFC-028 section 6 places extraction in the
        scan, and it exists at all because section 11 names the one reason
        to turn it off -- a measured per-file cost too high for a very
        large collection.

        `scopes` are relative to `root` and default to none at all, which
        means the whole root -- so every caller written before RFC-029
        keeps the behaviour it had. They are expected to be normalised
        already (`normalize_scopes()`): one scope nested inside another
        would have its files discovered twice, and a sequence that visits
        a file twice has no positions in it for a checkpoint to name.

        `resume_after` is a path relative to `root`, from a previous run's
        checkpoint. Files up to and including it are skipped, which saves
        the scan and not the inference -- the incremental decision already
        makes re-processing them cheap (RFC-029 section 10).

        `observer`, when given, is told how far the scan has got every
        `report_every` files examined, and once more when it ends. That
        exists for the heartbeat: a job whose files are all skipped by the
        incremental decision never completes a batch, and one that resumes
        skips most of its scan here without yielding anything -- both look
        dead to a reaper watching for batches (RFC-029 section 9.1). The
        provider still knows nothing about jobs; it reports to a port.

        `excluded_directories` are absolute directories whose contents are
        never yielded, wherever they sit under `root` (RFC-030). The one
        caller that needs it passes the thumbnail cache: indexing a whole
        system disk would otherwise discover every thumbnail as a photo,
        embed it, render a thumbnail *of* it, and discover that on the next
        run -- a collection that grows every time it is scanned. Excluded
        files are skipped before they count as examined, so they are not
        part of the sequence a checkpoint names either.
        """
        self._root = root
        self._supported_extensions = {ext.lower() for ext in supported_extensions}
        self._extract_capture_date = extract_capture_date
        self._scopes = scopes
        self._resume_after = resume_after
        self._observer = observer or NullIndexingObserver()
        self._report_every = max(1, report_every)
        self._excluded = tuple(
            directory.resolve() for directory in excluded_directories
        )

    def discover(self) -> Iterator[DiscoveredImageFile]:
        """Yield metadata for every supported image file the scan covers.

        The order is stable across runs, and RFC-029 section 10 depends on
        it -- see this module's docstring.

        With several scopes, each is walked in full before the next, and
        the scopes themselves are walked in path order. That ordering is
        not cosmetic: it is what keeps the whole sequence in path order
        *across* scope boundaries, because normalised scopes are disjoint
        and every file under a scope shares its leading parts. Walk the
        scopes in the order the client happened to send them and a resume
        that lands mid-sequence would compare against a checkpoint from a
        different ordering.

        The capture date is read in the same pass as `stat()`, not in a
        second walk and not lazily. A lazy field -- a callable on
        `DiscoveredImageFile` -- was considered and deferred until a
        measurement asks for it (RFC-028 section 11): it would move the
        read out of the scan and into whichever consumer happened to call
        it first, which is how a cost stops being visible.
        """
        yielded = 0
        examined = 0
        for scope_root in self._scope_roots():
            if not scope_root.exists():
                continue
            for discovered in self._discover_under(scope_root):
                examined += 1
                if discovered is not None:
                    yielded += 1
                    yield discovered
                if examined % self._report_every == 0:
                    self._observer.discovered(yielded, complete=False)

        self._observer.discovered(yielded, complete=True)

    def _scope_roots(self) -> list[Path]:
        """Return the directories to walk, in the order they will be walked.

        Sorted by the same key that orders files, so that the boundary
        between two scopes falls where the global path order puts it.
        """
        if not self._scopes:
            return [self._root]
        return sorted(
            (self._root / str(scope) for scope in self._scopes),
            key=discovery_sort_key,
        )

    def _discover_under(self, scope_root: Path) -> Iterator[DiscoveredImageFile | None]:
        """Walk one directory in path order, skipping anything already done.

        Yields `None` for a supported file the resume skipped, so that the
        caller can count it as examined without handing it downstream.
        That is not bookkeeping for its own sake: on a resume, almost the
        whole scan is skipped files, and a heartbeat driven by *yielded*
        files alone would go silent for exactly as long as the resume
        saves (RFC-029 section 9.1).

        **The `sorted()` is what this cannot cover.** It consumes the
        entire `rglob` before the first file comes out, so a cold walk of
        a large disk produces no callback at all until it finishes. That
        is the gap `job_stale_timeout` has to clear, and it is why
        RFC-029 section 9.1's timeout comes from a measured distribution
        that includes the first window rather than from the length of a
        batch.
        """
        for path in sorted(scope_root.rglob("*"), key=discovery_sort_key):
            if not path.is_file():
                continue
            if self._is_excluded(path):
                continue
            if path.suffix.lower() not in self._supported_extensions:
                continue
            if not is_after_checkpoint(self._relative(path), self._resume_after):
                yield None
                continue

            stat = path.stat()
            yield DiscoveredImageFile(
                path=path,
                filename=path.stem,
                extension=path.suffix.lower().lstrip("."),
                file_size=stat.st_size,
                file_modified_at=datetime.datetime.fromtimestamp(
                    stat.st_mtime, tz=datetime.UTC
                ),
                capture_date=(
                    read_capture_date(path) if self._extract_capture_date else None
                ),
            )

    def _is_excluded(self, path: Path) -> bool:
        """Whether `path` lies inside one of the excluded directories.

        Compared as paths, so the platform's case rule applies: on Windows
        `AppData/Local` and `appdata/local` are the same directory.

        The discovered path is *not* resolved, although the excluded ones
        are. Resolving is a system call per file on the one loop that runs
        100,000 times, and it buys nothing here: the walk starts from a
        resolved root and yields absolute paths spelled as the directory
        listing spells them, which is how the excluded directory resolves
        too.
        """
        return any(path.is_relative_to(excluded) for excluded in self._excluded)

    def _relative(self, path: Path) -> PurePath:
        """Express a discovered file the way a checkpoint names it.

        Relative to the scan root, which for a job is the device's mount
        point -- so the value compared here is the value stored, and the
        comparison is the one that ordered the scan.
        """
        return path.relative_to(self._root)
