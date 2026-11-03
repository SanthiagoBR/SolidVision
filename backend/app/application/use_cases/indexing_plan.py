"""The skip decision, extracted so one image and a batch can share it.

Before RFC-024 this logic lived inside `IndexOrUpdateImageUseCase.execute()`,
fused to the embed and the persist for a single image. Batch inference cannot
be built on top of that shape -- calling a single-image method N times in a
loop is still N single-image calls -- so the decision was pulled out into a
pure function that both the single-image use case and the batch coordinator
call.

"Pure" is load-bearing: given the candidate, the metadata already read back
from persistence, and a hasher, this function decides and returns. It performs
no writes, so the caller is free to run it over every discovered file first
and only then assemble batches from the survivors (RFC-024 section 8).
"""

from __future__ import annotations

import datetime
import enum
import weakref
from collections.abc import Callable
from dataclasses import dataclass

from app.domain.entities.image import Image
from app.domain.services.content_hasher_port import ContentHasherPort
from app.domain.value_objects.index_metadata import IndexMetadata


class FileContent:
    """The bytes of one file, read once, and their SHA-256 (RFC-045 section 5.3).

    Produced by the read-ahead thread, in Infrastructure, which read the
    whole file and hashed what it read; consumed here, where the hash
    answers step 3 of `plan_indexing()` and the bytes go to the embedding
    model and the thumbnail instead of the file being opened again. Each
    consumer still decodes for itself: what this carries is where the bytes
    come from, never a decoded picture (RFC-030 section 7.2).

    **The bytes are released explicitly**, by the pipeline, as soon as the
    file needs them no more (RFC-045 section 5.5): a decision that skips
    it, a failed hash, the write of the batch it went into. After
    `release()`, `data` raises rather than handing back something stale.
    `on_release` is told how many bytes went, once, on the first of
    `release()` or the garbage collection of this object -- a path that
    forgets to release still gives the bytes back, it only gives them back
    later. It is called with the size alone and holds no reference to this
    object, which is what lets the collection happen at all.
    """

    __slots__ = ("__weakref__", "_data", "_finalizer", "content_hash", "size")

    def __init__(
        self,
        content_hash: str,
        data: bytes,
        on_release: Callable[[int], None] | None = None,
    ) -> None:
        self.content_hash = content_hash
        """The SHA-256 of `data`, as `Sha256ContentHasher` writes it."""
        self.size = len(data)
        """How many bytes `data` holds, and still says so once released."""
        self._data: bytes | None = data
        self._finalizer = (
            weakref.finalize(self, on_release, self.size)
            if on_release is not None
            else None
        )

    @property
    def data(self) -> bytes:
        """The file's bytes; raises `RuntimeError` once they were released."""
        if self._data is None:
            raise RuntimeError("The bytes of this file were already released")
        return self._data

    @property
    def released(self) -> bool:
        """Whether `release()` was called."""
        return self._data is None

    def release(self) -> None:
        """Drop the bytes and give them back to whoever counts them. Idempotent."""
        self._data = None
        if self._finalizer is not None:
            self._finalizer()

    def __repr__(self) -> str:
        state = "released" if self.released else f"{self.size} bytes"
        return f"FileContent({self.content_hash[:12]}..., {state})"


@dataclass(frozen=True)
class IndexCandidate:
    """One discovered file, as the Application layer sees it.

    Carries the filesystem facts the skip decision compares against, and
    nothing about how the file was found -- the Infrastructure type that
    produced it (`DiscoveredImageFile`) stays in Infrastructure.

    The capture date the scan read (RFC-028) travels on `image`, where it
    is a field of the entity, rather than as two more fields here beside
    `file_size` and `file_modified_at` -- and so does the position
    (RFC-032). That placement is also a guard: the fields of this class
    are the change signals `plan_indexing()` compares, and neither a
    capture date nor a position is one (RFC-028 section 6.1, RFC-032
    section 8).
    """

    image: Image
    file_size: int | None
    file_modified_at: datetime.datetime | None
    content: FileContent | None = None
    """The file's bytes and their hash, when the read-ahead read them (RFC-045).

    `None` -- every candidate built before RFC-045, the CLI with the
    read-ahead off, the backfills, the tests -- means the pipeline works as
    it did: the hash and the decodes read the file from its location. Not a
    change signal either: `plan_indexing()` takes its hash from here only
    after steps 1 and 2 have decided that a hash is needed.
    """


class IndexAction(enum.Enum):
    """What the three-step incremental check concluded about one file."""

    EMBED = "embed"
    """Content is new or genuinely changed; generate an embedding."""

    SKIP_UNCHANGED = "skip_unchanged"
    """Size and mtime both match; nothing to do, and nothing was read."""

    REFRESH_METADATA = "refresh_metadata"
    """Size or mtime moved but the bytes did not.

    The stored embedding is still correct, so only the filesystem metadata
    that drives the next run's comparison needs to catch up. This is the
    outcome that ARCHITECTURE.md section 16 step 3 exists to produce, and
    the one RFC-024 counts separately in its summary: every file landing
    here would have been re-embedded before this RFC.
    """


@dataclass(frozen=True)
class IndexPlan:
    """A decision about one candidate, plus the hash that justified it."""

    candidate: IndexCandidate
    action: IndexAction
    content_hash: str | None


def is_unchanged(
    candidate: IndexCandidate,
    file_size: int | None,
    file_modified_at: datetime.datetime | None,
) -> bool:
    """Step 2 of the incremental check: both stored change signals match.

    Extracted from `plan_indexing()` by RFC-045 section 5.2, because a
    second caller needs the same rule: the read-ahead thread, which decides
    from a snapshot of the stored signals whether the decision will ask for
    a file's bytes, before it reads them. One function, so the rule of "not
    changed" stays in one place.

    A stored `None` never matches a value the scan read, which is a plain
    consequence of `None != int` -- a row that never had the columns is
    treated as changed, never as unchanged.
    """
    return (
        file_size == candidate.file_size
        and file_modified_at == candidate.file_modified_at
    )


def plan_indexing(
    candidate: IndexCandidate,
    existing: IndexMetadata | None,
    content_hasher: ContentHasherPort,
) -> IndexPlan:
    """Run ARCHITECTURE.md section 16's cost-ascending check on one file.

    1. No row at all (`existing is None`) -> new file.
    2. Row exists and both `file_size` and `file_modified_at` match -> skip,
       having read nothing but the directory entry.
    3. Otherwise the file is a *candidate* for reprocessing, so -- and only
       so -- read its bytes and hash them. A stored hash that matches means
       the content never changed and the embedding can stand.

    The hash is also computed for a file with no row yet, which step 1 has
    already condemned to embedding. That is not a violation of "never hash
    before the timestamp and size checks": the checks have already run and
    already returned "process this". It is what makes step 3 possible on
    the *next* run -- a row persisted without a hash reads back as `None`,
    which means unknown, so skipping the hash here would leave the whole
    mechanism permanently dormant for every file the system ever indexes.

    **The capture date is not consulted, and must never be** (RFC-028
    section 6.1). `existing.capture_source` is in the prefetched metadata
    for the conditional date write, not for this decision: a new EXIF date
    on identical pixels is a metadata change, and answering it with a
    re-embed would spend the most expensive operation in the system on
    it. `tests/application/test_indexing_plan.py` fails if a comparison is
    added.

    **Neither is the position** (RFC-032 section 8), and for the same
    reason: `existing.position_source` rides the prefetch for the
    conditional position write. A coordinate read today describes the same
    pixels it described yesterday. The tests vary that field on the stored
    metadata and were checked by mutation -- adding
    `existing.position_source == candidate.image.position_source` to the
    first condition makes them fail.

    A stored hash of `None` never matches a computed one, which is a plain
    consequence of `None != str` rather than a special case. Rows written
    before RFC-024 carry NULL and were deliberately not backfilled
    (RFC-024 section 15), so "unknown" has to cost one re-embed rather than
    risk a wrongly skipped one.

    **The hash of step 3 comes from `candidate.content` when there is one**
    (RFC-045 section 5.3): the read-ahead already read the file and hashed
    those bytes, with the same SHA-256 the hasher computes. Without it the
    hasher reads the file, as before. Same three outcomes either way, and
    the bytes are not read for a file step 2 skips -- `content` is only
    looked at after it.
    """
    if existing is not None and is_unchanged(
        candidate, existing.file_size, existing.file_modified_at
    ):
        return IndexPlan(
            candidate=candidate,
            action=IndexAction.SKIP_UNCHANGED,
            content_hash=existing.content_hash,
        )

    content_hash = (
        candidate.content.content_hash
        if candidate.content is not None
        else content_hasher.hash_image(candidate.image)
    )

    if existing is not None and existing.content_hash == content_hash:
        return IndexPlan(
            candidate=candidate,
            action=IndexAction.REFRESH_METADATA,
            content_hash=content_hash,
        )

    return IndexPlan(
        candidate=candidate,
        action=IndexAction.EMBED,
        content_hash=content_hash,
    )
