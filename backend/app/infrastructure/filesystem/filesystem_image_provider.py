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
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePath

from app.domain.services.indexing_observer import (
    IndexingObserver,
    NullIndexingObserver,
)
from app.domain.value_objects.job_scope import JobScope
from app.domain.value_objects.root_listing import RootListing
from app.infrastructure.filesystem.discovered_image_file import DiscoveredImageFile
from app.infrastructure.filesystem.exif_capture_date import read_exif_facts
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)


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


@dataclass(frozen=True)
class ListedTree:
    """What `_list_tree()` found under one directory.

    `entries` are what `sorted(root.rglob("*"))` used to give -- folders and
    files, without the root itself -- unsorted; `files` are the entries the
    walk classed as files, **the same objects** as in `entries`;
    `unreadable` are the folders whose listing failed, absolute, in the order
    they failed.
    """

    entries: list[Path]
    files: list[Path]
    unreadable: list[Path]

    def file_identities(self) -> set[int]:
        """The `id()` of each file, to ask "is this entry a file" of `entries`.

        **By identity, not by path.** Hashing a `WindowsPath` case-folds its
        text, every time: a `set` of the 116,396 paths of `E:` was 1.5 s of
        the 2.2 s the first listing took, and made the walk twice as slow as
        the `rglob` it replaces (RFC-046 section 16.12). The objects are the
        ones in `entries`, alive as long as this tree is, so their ids are
        theirs.
        """
        return {id(path) for path in self.files}


def _list_tree(scope_root: Path) -> ListedTree:
    """List everything under `scope_root`, and say which folders could not be read.

    **`Path.walk(on_error=...)`, not `rglob("*")`** (RFC-046 section 5.2). In
    the Python 3.12 of this installation `rglob("*")` finds the folders with a
    `walk()` that has no `on_error`, and lists each one again with a
    `scandir()` inside an `except OSError: pass` -- so a folder that cannot be
    read vanishes from the listing, with everything in it, and nothing says
    so. The pilot of section 2 showed it with a folder denied by `icacls`.
    The walk gives the same entries and hands the failure here.

    What does not change, and why:

    * **the entries**: the walk's `dirnames` and `filenames` of every folder
      are what the `scandir()` of the `rglob` listed;
    * **the links**: `follow_symlinks=False`, which is what the `rglob` used
      inside; a link to a folder is an entry and is not walked into;
    * **the order**: none here. It comes from the `sorted()` with
      `discovery_sort_key` after the listing, as before.

    Two behaviours of the 3.12 `walk()` the callers rely on (section 5.2): an
    error **while reading** a folder that did open is not given to `on_error`
    -- it propagates, ends the scan, and the job fails, which marks nothing;
    and an `is_dir()` that fails classes the entry as a file, whose folder is
    then not walked into. On Windows the type comes from the listing itself.

    One read per folder, where the `rglob` made two.
    """
    entries: list[Path] = []
    files: list[Path] = []
    unreadable: list[Path] = []

    def note(error: OSError) -> None:
        # `filename` is the folder whose `scandir()` failed. Without one the
        # failure has no place, and the conservative answer is the root:
        # everything under it is then out of the sweep's reach.
        failed = None if error.filename is None else Path(error.filename)
        if failed is None or not failed.is_relative_to(scope_root):
            failed = scope_root
        unreadable.append(failed)

    for folder, dirnames, filenames in scope_root.walk(on_error=note):
        for name in dirnames:
            entries.append(folder / name)
        for name in filenames:
            path = folder / name
            entries.append(path)
            files.append(path)
    return ListedTree(entries=entries, files=files, unreadable=unreadable)


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
    since RFC-028, each file's EXIF header via Pillow -- once per file,
    for both the date and, since RFC-032, the position. Since RFC-029 it
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
        extract_gps: bool = True,
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

        `extract_gps` is `extract_capture_date`'s twin for RFC-032, in the
        same mould and for the same reasons: passed in from
        `settings.extract_gps`, on by default, and off means "not
        examined", never "no position". The two are separate switches over
        **one** header read: with either on, each file is opened once and
        both requested facts come out of that open; with both off, the file
        is not opened at all. Added last, as a keyword, so no existing
        caller's positional arguments move.
        """
        self._root = root
        self._supported_extensions = {ext.lower() for ext in supported_extensions}
        self._extract_capture_date = extract_capture_date
        self._extract_gps = extract_gps
        self._scopes = scopes
        self._resume_after = resume_after
        self._observer = observer or NullIndexingObserver()
        self._report_every = max(1, report_every)
        self._excluded = tuple(
            directory.resolve() for directory in excluded_directories
        )
        # `pathlib`'s own rule: `PurePath` equality folds case where the
        # platform does (`_str_normcase`), and so does `_is_excluded()`.
        self._case_insensitive = PurePath("A") == PurePath("a")
        self._excluded_texts = tuple(
            (folded, folded if folded.endswith(os.sep) else folded + os.sep)
            for folded in (self._fold(str(directory)) for directory in self._excluded)
        )
        root_text = str(root)
        self._root_prefix = (
            root_text if root_text.endswith(os.sep) else root_text + os.sep
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

        The capture date and the position are read in the same pass as
        `stat()`, from one open of the file (RFC-032 section 4.2), not in a
        second walk and not lazily. A lazy field -- a callable on
        `DiscoveredImageFile` -- was considered and deferred until a
        measurement asks for it (RFC-028 section 11): it would move the
        read out of the scan and into whichever consumer happened to call
        it first, which is how a cost stops being visible.

        A scan of the whole root -- no scopes -- also tells the observer
        how many supported files its listing holds, once, before the
        first file comes out (`IndexingObserver.listed()`, RFC-039 section
        4.1). A scan of some folders never does.
        """
        yielded = 0
        examined = 0
        whole_root = not self._scopes
        for scope_root in self._scope_roots():
            if not scope_root.exists():
                continue
            for discovered in self._discover_under(scope_root, report=whole_root):
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

    def _discover_under(
        self, scope_root: Path, report: bool = False
    ) -> Iterator[DiscoveredImageFile | None]:
        """Walk one directory in path order, skipping anything already done.

        Yields `None` for a supported file the resume skipped, so that the
        caller can count it as examined without handing it downstream.
        That is not bookkeeping for its own sake: on a resume, almost the
        whole scan is skipped files, and a heartbeat driven by *yielded*
        files alone would go silent for exactly as long as the resume
        saves (RFC-029 section 9.1).

        **The `sorted()` is what this cannot cover.** It consumes the
        entire walk before the first file comes out, so a cold walk of
        a large disk produces no callback at all until it finishes. That
        is the gap `job_stale_timeout` has to clear, and it is why
        RFC-029 section 9.1's timeout comes from a measured distribution
        that includes the first window rather than from the length of a
        batch.

        With `report`, the listing goes to `IndexingObserver.listed()` as
        soon as it exists and before the first file is yielded: that is
        when the denominator of "% indexed" is known (RFC-039 section 4.1),
        and since RFC-046 the files themselves, which the job compares with
        the device's rows when it ends (`RootListing`).

        **A folder that could not be read is a WARNING, in every scan** --
        of some folders too -- with its name (RFC-046 section 5.2). Before,
        it left no trace. One inside an excluded directory is not reported:
        nothing there is listed anyway, and `System Volume Information` is
        unreadable on every disk.
        """
        tree = _list_tree(scope_root)
        unreadable = [
            folder for folder in tree.unreadable if not self._is_excluded(folder)
        ]
        for folder in unreadable:
            logger.warning(
                "Could not read the folder %s; nothing under it was listed",
                self._posix(folder),
            )
        listing = sorted(tree.entries, key=discovery_sort_key)
        if report:
            self._observer.listed(
                self._root_listing(listing, tree.file_identities(), unreadable)
            )
        for path in listing:
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
            facts = read_exif_facts(
                path,
                capture_date=self._extract_capture_date,
                position=self._extract_gps,
            )
            yield DiscoveredImageFile(
                path=path,
                filename=path.stem,
                extension=path.suffix.lower().lstrip("."),
                file_size=stat.st_size,
                file_modified_at=datetime.datetime.fromtimestamp(
                    stat.st_mtime, tz=datetime.UTC
                ),
                capture_date=facts.capture_date,
                position=facts.position,
            )

    def _root_listing(
        self, listing: list[Path], files: set[int], unreadable: list[Path]
    ) -> RootListing:
        """The listing a whole-root scan reports: its photographs, and its holes.

        The files are the entries the walk classed as files, with a
        supported extension, outside the excluded directories -- the
        walk's own tests, and **no `stat`**: the walk already knows a
        folder from a file, so a folder named `x.jpg` no longer counts
        (RFC-046 section 5.2, which removes the imprecision RFC-039 section
        4.1 declared). In listing order, as text relative to the root.

        Every file is in it, the ones before a checkpoint too: the
        denominator is the size of the disk, and the sweep compares the
        whole disk; a resume lists the whole tree again.
        """
        photographs = tuple(
            self._posix(path)
            for path in listing
            if path.suffix.lower() in self._supported_extensions
            and id(path) in files
            and not self._is_excluded(path)
        )
        return RootListing(
            file_count=len(photographs),
            files=photographs,
            unreadable=tuple(self._posix(folder) for folder in unreadable),
        )

    def _posix(self, path: Path) -> str:
        """`path` relative to the scan root, in the posix form `ImagePath` keeps.

        **The text after the root's, not `relative_to()`.** Every path of a
        listing is the root joined with names, so its text begins with the
        root's; `relative_to()` reaches the same answer by walking the
        parents, and on the 123,353 files of `E:` that was 6.2 s per scan
        (RFC-046 section 16.12). A path that does not begin with the root's
        text -- the root itself, or one spelled otherwise -- goes the long
        way.
        """
        text = str(path)
        if text.startswith(self._root_prefix) and len(text) > len(self._root_prefix):
            return text[len(self._root_prefix) :].replace(os.sep, "/")
        return path.relative_to(self._root).as_posix()

    def _is_excluded(self, path: Path) -> bool:
        """Whether `path` lies inside one of the excluded directories.

        Compared as paths, so the platform's case rule applies: on Windows
        `AppData/Local` and `appdata/local` are the same directory.

        **As text, by the rule `pathlib` uses** (RFC-046 section 16.12):
        the path's text, folded as `pathlib` folds it for its own
        comparisons, either is the excluded directory's or begins with it
        and a separator -- which is `is_relative_to()` for the absolute,
        `..`-free paths a walk yields, and the separator is what keeps
        `cache` from excluding `cache2`. `is_relative_to()` builds every
        parent of the path to compare it; with the three directories of
        RFC-046 that was 8.9 s on the files of `E:`, twice per scan.

        The discovered path is *not* resolved, although the excluded ones
        are. Resolving is a system call per file on the one loop that runs
        100,000 times, and it buys nothing here: the walk starts from a
        resolved root and yields absolute paths spelled as the directory
        listing spells them, which is how the excluded directory resolves
        too.
        """
        text = self._fold(str(path))
        return any(
            text == excluded or text.startswith(prefix)
            for excluded, prefix in self._excluded_texts
        )

    def _fold(self, text: str) -> str:
        """`text` in the case `pathlib` compares paths in, on this platform."""
        return text.lower() if self._case_insensitive else text

    def _relative(self, path: Path) -> PurePath:
        """Express a discovered file the way a checkpoint names it.

        Relative to the scan root, which for a job is the device's mount
        point -- so the value compared here is the value stored, and the
        comparison is the one that ordered the scan.
        """
        return path.relative_to(self._root)
