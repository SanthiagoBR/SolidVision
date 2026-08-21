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
from dataclasses import dataclass

from app.domain.entities.image import Image
from app.domain.services.content_hasher_port import ContentHasherPort
from app.domain.value_objects.index_metadata import IndexMetadata


@dataclass(frozen=True)
class IndexCandidate:
    """One discovered file, as the Application layer sees it.

    Carries the filesystem facts the skip decision compares against, and
    nothing about how the file was found -- the Infrastructure type that
    produced it (`DiscoveredImageFile`) stays in Infrastructure.
    """

    image: Image
    file_size: int | None
    file_modified_at: datetime.datetime | None


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

    A stored hash of `None` never matches a computed one, which is a plain
    consequence of `None != str` rather than a special case. Rows written
    before RFC-024 carry NULL and were deliberately not backfilled
    (RFC-024 section 15), so "unknown" has to cost one re-embed rather than
    risk a wrongly skipped one.
    """
    if (
        existing is not None
        and existing.file_size == candidate.file_size
        and existing.file_modified_at == candidate.file_modified_at
    ):
        return IndexPlan(
            candidate=candidate,
            action=IndexAction.SKIP_UNCHANGED,
            content_hash=existing.content_hash,
        )

    content_hash = content_hasher.hash_image(candidate.image)

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
