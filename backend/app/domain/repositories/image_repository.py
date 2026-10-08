"""Abstract repository port for image persistence operations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence

from app.domain.entities.image import Image
from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord
from app.domain.value_objects.job_scope import JobScope
from app.domain.value_objects.position import (
    BoundingBox,
    PositionCells,
    PositionReading,
)
from app.domain.value_objects.search_filters import SearchFilters
from app.domain.value_objects.search_hit import SearchHits


class ImageRepository(ABC):
    """Repository contract for managing Image domain entities."""

    @abstractmethod
    def save(self, image: Image) -> None:
        """Persist a new image in the repository.

        Kept as the original RFC-015 create-once contract, used by
        `IndexImageUseCase`. Incremental indexing uses `save_indexed()`
        instead.
        """

    @abstractmethod
    def get(self, image_id: ImageId) -> Image | None:
        """Retrieve an image by its identifier."""

    @abstractmethod
    def exists(self, image_id: ImageId) -> bool:
        """Return whether an image exists for the provided identifier."""

    @abstractmethod
    def delete(self, image_id: ImageId) -> None:
        """Remove an image from the repository."""

    @abstractmethod
    def list(self) -> list[Image]:
        """Return all images known to the repository."""

    @abstractmethod
    def save_indexed(self, record: IndexingRecord) -> None:
        """Create or update an image together with its embedding and metadata.

        The whole row is written from the record, capture date included:
        `record.image.captured_at` and `capture_source` replace whatever
        was stored, even when they are `None`. That is correct for the only
        caller, which reaches this for new or genuinely changed bytes --
        a date read from the old bytes no longer describes the file, and
        "never examined" is the honest state until a scan reads the new
        ones.

        The same holds for `record.thumbnail_path` (RFC-030): written as
        given, `None` included, because a thumbnail rendered from the old
        bytes no longer depicts the file -- and for the position (RFC-032):
        `latitude`, `longitude` and `position_source` come from the entity,
        `None` included, for the capture date's reason.
        """

    @abstractmethod
    def save_indexed_many(self, records: Sequence[IndexingRecord]) -> None:
        """Create or update many images as a single unit of work.

        All or nothing: either every record is persisted or none is. That
        is what makes the method worth having -- one commit instead of N --
        and it is also what makes it dangerous, since a single bad row
        discards the embeddings of every other row in the batch, each of
        which cost real inference time to produce.

        Callers are therefore expected to fall back to `save_indexed()` per
        record when this raises, so that only the genuinely bad row is
        lost (RFC-024 section 7.2). An implementation must leave itself
        usable for exactly that: on failure it must roll back cleanly
        rather than leaving half-applied state behind.

        Constraint violations propagate as they do from `save_indexed()`;
        they are real errors, not duplicate-create signals.
        """

    @abstractmethod
    def search_similar(
        self,
        embedding: EmbeddingVector,
        limit: int,
        filters: SearchFilters | None = None,
    ) -> SearchHits:
        """Return the images closest to `embedding`, best match first.

        The counterpart of `save_indexed()`: that method is how an
        embedding enters the repository, and this is the only way one is
        used once it is there. The repository owns the ranking -- callers
        receive an ordered list and must not re-sort it, since an
        implementation is free to rank by whatever it stores rather than
        by recomputing anything in the caller's process.

        The contract every implementation owes:

        - Results are ordered from most similar to least similar, and
          `SearchHit.similarity` is cosine similarity in [-1, 1]. It is
          never rescaled to [0, 1]; a negative score is a real answer
          meaning the vectors oppose each other.
        - An image with no stored embedding never appears. It was never
          compared, and inventing a score for it would put unindexed
          files in front of indexed ones.
        - At most `limit` hits come back, fewer when fewer images have
          embeddings, and `[]` when none do. A short result means the
          repository ran out of candidates, never that it ran out of
          quality: filtering by a minimum score is not part of this
          contract.
        - Equally similar images come back in a stable, deterministic
          order, so that repeating a search repeats its result.
        - Without `filters`, the search covers every image known to the
          repository. RFC-025 wrote that there was "no scoping argument,
          by collection or otherwise" and named the condition for adding
          one: a real table, a real foreign key, and ownership rules.
          RFC-027 delivers those for *devices*, which are a physical fact
          about where bytes are rather than a modelling choice, so
          `filters` narrows by device -- and, since RFC-028, by capture
          date, which is a fact recorded inside the file.
        - A capture-date range is half-open, `[start, end)`, over the
          camera-local `captured_at`. An image whose capture date is
          unknown never matches a range, however wide: unknown never
          means "matches" (RFC-020). The device set and the range combine
          with AND.
        - A circle (RFC-032) keeps the images whose position is within
          `radius_m` metres of its centre by **exact haversine distance**,
          the edge included, computed with `EARTH_RADIUS_M`. A bounding box
          may narrow the rows first, but only as a pre-filter that can never
          remove a row the distance would keep. An image whose position is
          unknown never matches a circle, however large -- the in-memory
          implementations must say `is None` explicitly, because Python
          raises where SQL quietly compares NULL as false. All three
          clauses combine with AND.
        - `filters=None` and `filters=SearchFilters()` mean the same
          thing: no narrowing, and byte-for-byte the query RFC-025
          shipped. Neither may be read as "an empty set of devices",
          which would match nothing and turn a defaulted argument into a
          search that silently returns zero results -- nor as an
          unbounded date range, which would silently drop every image
          without a date.
        - Every hit's `Image` carries its `captured_at` and
          `capture_source`, so a caller can show why a photo matched a
          date filter and how much that date is worth -- and, since
          RFC-032, its `latitude`, `longitude` and `position_source`, for
          the same reason about a circle.
        - A filtered search obeys every rule above, including ordering,
          the cosine range, and skipping images with no embedding. It
          restricts the candidate set; it does not change the ranking
          within it.
        - Filtering on a device that has no images returns `[]`, and
          filtering on an unknown device id is not an error. The set is a
          restriction, not an assertion that its members exist.
        - An `embedding` whose width does not match the indexed vectors
          raises `EmbeddingDimensionMismatchError`. It is a caller error,
          not a search that happens to match nothing, and it must fail
          identically in every implementation -- the plausible bug is an
          implementation that truncates to the shorter vector and returns
          a confident, meaningless ranking.

        `limit` is expected to be a positive number the caller has already
        vetted; policy about how large a page may be belongs to the
        Application layer, not here.

        **A filter may cost recall, and that is measured rather than
        assumed.** PostgreSQL does not push an arbitrary predicate into
        an HNSW graph traversal; it applies the filter to what the index
        already returned, so a scan that explored `ef_search` candidates
        can hand back *fewer* than `limit` rows while more matching rows
        exist. That is the same mechanism RFC-025 section 7.3 measured by
        accident with dead tuples. The filter is justified by usefulness
        -- "search only the disk in my hand" -- not by speed (RFC-027
        section 9.1).
        """

    @abstractmethod
    def get_index_metadata(self, image_id: ImageId) -> IndexMetadata | None:
        """Return the persisted filesystem metadata for an image, if any.

        Returns `None` when no row exists for `image_id` (new file).
        Returns `IndexMetadata(None, None)` when a row exists but has no
        metadata yet -- callers must treat that as changed, not unchanged.

        Includes the row's `capture_source` (RFC-028), `thumbnail_path`
        (RFC-030) and `position_source` (RFC-032), none of which is a
        change signal; see `IndexMetadata`.
        """

    @abstractmethod
    def get_index_metadata_many(
        self, image_ids: Sequence[ImageId]
    ) -> dict[ImageId, IndexMetadata]:
        """Return the persisted filesystem metadata for many images at once.

        The bulk counterpart of `get_index_metadata()`, and the reason
        RFC-024 added it: a re-index over an unchanged collection asks the
        skip question once per discovered file, so the per-file version
        turns a 100,000-image scan into 100,000 round trips *before* any
        inference happens. That is a Big-O problem in the number of files,
        independent of how fast the model is.

        Ids with no row are simply absent from the result -- the mapping is
        not padded with `None` values. Callers must therefore distinguish
        "absent" (new file) from "present with `None` fields" (row exists,
        metadata never written) exactly as they do for the single-id
        version.
        """

    @abstractmethod
    def update_index_metadata(self, image_id: ImageId, metadata: IndexMetadata) -> None:
        """Refresh an existing row's filesystem metadata, leaving its embedding.

        The write half of the content-hash check (ARCHITECTURE.md 16 step
        3): when a file's mtime or size moved but its bytes did not, the
        stored embedding is still correct and re-computing it would be pure
        waste. Only the metadata that drives the *next* skip decision needs
        to catch up.

        Does nothing when no row exists for `image_id`; a caller that wants
        a row created must go through `save_indexed()`, which is the only
        contract carrying an embedding.

        Writes the three change signals and nothing else. In particular it
        does not touch the capture date, whatever `metadata.capture_source`
        says: that belongs to `update_capture_date()`, and a refresh that
        also wrote it would reset an examined row to "never examined"
        whenever a caller built its `IndexMetadata` without one. The
        position is left alone for the same reason, whatever
        `metadata.position_source` says (RFC-032 section 8), and so is the
        thumbnail location -- a refresh means the bytes did not change, so
        the thumbnail still depicts them.
        """

    @abstractmethod
    def update_capture_date(self, image_id: ImageId, capture: CaptureDate) -> None:
        """Record the examined capture date of an existing row (RFC-028).

        Writes `captured_at` and `capture_source` and nothing else -- no
        embedding, no change signal -- because a capture date is a
        searchable attribute of the photograph, not a reason to reindex it
        (RFC-028 section 6.1). This is how an already-indexed image gains a
        date without paying for inference.

        Takes a `CaptureDate`, never `None`: there is no call that marks a
        row as "never examined" again, since nothing legitimate un-reads a
        file.

        Does nothing when no row exists for `image_id`, the same contract
        as `update_index_metadata()`. A date is only worth storing beside
        an image the system knows, and creating a row here would create
        one with no embedding.

        Unconditional. Deciding *whether* to write -- only rows never
        examined during a scan; never replacing a stronger source with a
        weaker one under the backfill's `--force` -- is the caller's
        policy, in `capture_date_to_write()`.
        """

    @abstractmethod
    def update_capture_date_many(self, captures: Mapping[ImageId, CaptureDate]) -> None:
        """Record many capture dates as one write.

        The bulk counterpart of `update_capture_date()`, for the reason
        RFC-024 added `get_index_metadata_many()`: the first scan after
        RFC-028 dates an entire already-indexed collection, and one
        statement per file would turn that pass into 100,000 round trips.

        Ids with no row are skipped silently, exactly as the single-row
        version does. An empty mapping writes nothing.
        """

    @abstractmethod
    def update_position(self, image_id: ImageId, reading: PositionReading) -> None:
        """Record the examined position of an existing row (RFC-032 section 8).

        Writes `latitude`, `longitude` and `position_source` and nothing
        else -- no embedding, no change signal, no capture date -- because a
        position is a searchable attribute of the photograph, not a reason
        to reindex it. This is how an already-indexed image gains a
        position without paying for inference.

        Takes a `PositionReading`, never `None`: nothing legitimate un-reads
        a file, so there is no call that marks a row "never examined" again.

        Does nothing when no row exists for `image_id`, the contract of
        `update_capture_date()`.

        Unconditional. Deciding *whether* to write is the caller's policy,
        in `position_to_write()`.
        """

    @abstractmethod
    def update_position_many(self, readings: Mapping[ImageId, PositionReading]) -> None:
        """Record many positions as one write.

        The bulk counterpart of `update_position()`, used once per prefetch
        window for the reason `update_capture_date_many()` exists: the
        first scan after RFC-032 places an entire already-indexed
        collection, and one statement per file would be 100,000 round
        trips. Ids with no row are skipped silently; an empty mapping
        writes nothing.
        """

    @abstractmethod
    def update_thumbnail_path(self, image_id: ImageId, location: str) -> None:
        """Record where an existing row's thumbnail was stored (RFC-030).

        The thumbnail backfill's write: it gives an already-indexed image
        a thumbnail without touching its embedding or its change signals,
        the way `update_capture_date()` gives it a date.

        Does nothing when no row exists for `image_id`. A thumbnail is only
        worth pointing at from an image the system knows, and creating a
        row here would create one with no embedding.
        """

    @abstractmethod
    def update_thumbnail_path_many(self, locations: Mapping[ImageId, str]) -> None:
        """Record many thumbnail locations as one write.

        The bulk counterpart of `update_thumbnail_path()`, used once per
        prefetch window by the backfill for the reason
        `update_capture_date_many()` exists. Ids with no row are skipped
        silently; an empty mapping writes nothing.
        """

    @abstractmethod
    def count_unknown_capture_date(self, filters: SearchFilters) -> int:
        """Count the searchable images a date filter hid for having no date.

        The number behind "N photos were left out because their date is
        unknown". RFC-028 section 4.1 requires the UI to be able to say it:
        without it, "I can't find the 2018 photo" and "the 2018 photo is
        indexed but has no EXIF" look identical -- an empty result that
        reads as the photo not existing, which is the failure RFC-028
        section 2.1 exists to remove.

        Counts images that (a) have an embedding, so a search could have
        returned them, (b) satisfy every *other* clause of `filters` -- the
        device set and, since RFC-032, the circle -- and (c) have
        `captured_at` NULL, whether never examined or examined without a
        date. When `filters.captured_between` is `None` there was no date
        clause to hide anything, the answer is 0, and an implementation
        must return it without querying.

        Three properties to know before "fixing" what looks inconsistent:

        - It is a **second query**, not a by-product of the search. A
          vector search returns at most `limit` rows and has no way to
          know what its `WHERE` discarded.
        - It counts over **the whole table under the filters**, not over
          the neighbourhood an approximate index happened to explore. That
          is the question the user is asking -- how many photos did the
          filter hide? -- and it is deliberately not the ranking's
          universe. The two numbers are not supposed to add up to anything.
        - It ignores the query text. Relevance is not the reason these
          images were left out; their missing date is.
        """

    @abstractmethod
    def count_unknown_position(self, filters: SearchFilters) -> int:
        """Count the searchable images that have no position (RFC-032 section 6.2).

        The number behind "N photos were left out because they have no
        coordinates". Without it a circle answers with the most expensive
        lie the product can tell -- *there is no photo of this place* --
        when the truth is *1,200 photos have no coordinates and none of
        them was looked at*.

        Counts images that (a) have an embedding, (b) satisfy every *other*
        clause of `filters` -- the device set and the capture-date range --
        and (c) have `latitude` NULL, whether never examined or examined
        without a fix. `filters.taken_within` is ignored: it is the clause
        whose victims are being counted.

        **Unlike `count_unknown_capture_date()`, this does not answer 0 for
        a filter without a circle.** `GET /images/map` asks exactly this
        question with no circle at all (RFC-032 section 7) -- how many
        photos the map cannot draw -- and it must be the same number over
        the same universe. Not paying for the query on a search with no
        circle is still a rule; it is enforced one layer up, where
        `SearchImagesUseCase` does not call this method at all.

        The three properties of `count_unknown_capture_date()` hold: a
        second query, over the whole table under the filters rather than
        the neighbourhood an approximate index explored, ignoring the
        query text.
        """

    @abstractmethod
    def aggregate_positions(
        self,
        filters: SearchFilters,
        area: BoundingBox,
        precision: int,
        cell_limit: int | None,
    ) -> PositionCells:
        """Group the searchable images in `area` into map cells (RFC-032 section 7).

        A map cannot be drawn from search hits: the top 100 of a 100,000-
        image index says nothing about where the photos are, and moving
        100,000 points to the client to group them is moving the collection
        across the wire to answer a question `GROUP BY` answers here.

        The universe is **the filters', not the ranking's**: images with an
        embedding, satisfying the device set and the capture-date range of
        `filters`, whose position lies inside `area` (edges included). That
        is the universe of `count_unknown_position()` restricted to images
        that have a position -- the two numbers describe one population,
        split by whether it can be drawn. There is no query text; ranking
        is the search's business.

        Each cell is the pair of coordinates rounded to `precision` decimal
        places, half away from zero, **as PostgreSQL rounds a
        `double precision` cast to `numeric`** -- the value's 15 significant
        digits rounded in decimal, not the binary float rounded by Python's
        `round()`. The in-memory implementations reproduce that exactly
        (`round_to_cell()`), or a photo on a cell boundary would land in
        different cells in different implementations.

        At most `cell_limit` cells come back -- every cell when it is
        `None` -- ordered by latitude and then longitude so a truncated
        answer is still deterministic. A caller that needs to know whether
        the area holds *more* than N cells asks for N + 1: the ceiling is
        the caller's policy (`MAX_MAP_CELLS`), and this method only stops
        counting past it.

        `filters.taken_within` is not consulted: the map's area is the
        viewport, and "near here" is a different question (RFC-032
        section 9).
        """

    @abstractmethod
    def count_by_device(self) -> dict[DeviceId, int]:
        """Return how many images each device has, as one grouped read.

        **A mapping, not a count for one id, and the plural is the
        contract.** `GET /api/v1/devices` renders every disk at once, so
        a single-device version of this would be N queries behind a
        signature that looks like one -- the same defect as enumerating
        volumes once per device, one layer down (RFC-031 section 4.3 of
        the build prompt).

        Devices with no images are **absent** rather than present with a
        zero, the way `get_index_metadata_many()` omits ids with no row.
        Callers read it with `.get(device_id, 0)`; padding it here would
        require this method to know which devices exist, which is the
        other repository's question.

        Counts every row, whether or not it carries an embedding. The
        number answers *"how many files of this disk does the system
        know"*, which is what sits beside `last_scan_file_count` in the
        response, and a row without an embedding is still a file this
        system knows about.
        """

    @abstractmethod
    def count_by_path_prefixes(
        self, device_id: DeviceId, parent: JobScope
    ) -> dict[str, int]:
        """Count images per immediate subfolder of `parent`, in one query.

        The read behind `GET /api/v1/devices/{id}/folders`: a folder
        listing shows `indexed_images` per row, and forty rows must not
        be forty `COUNT(*)`s (RFC-031 section 8.2).

        The result maps **one path component** -- the first segment below
        `parent` -- to the number of rows anywhere beneath it. `parent`
        being the whole-device scope groups by the first segment of
        `relative_path` itself.

        Two properties an implementation must reproduce exactly, because
        the contract test pins both and the natural implementations
        differ on them:

        * **Images sitting directly in `parent` appear under their own
          filename.** `2018/loose.jpg` under `parent="2018"` yields the
          key `loose.jpg`, which is a file and not a folder. That is
          deliberate rather than tolerated: filtering it out here would
          mean this method deciding what is a directory, which it cannot
          know without touching the disk. The caller already holds the
          folder names the filesystem reported and keeps only those keys,
          so these entries are discarded one layer up, together with the
          other kind of key that must not become a row.
        * **The other kind: a folder renamed on disk since it was
          indexed.** Its rows are still filed under the old name, so the
          old name comes back here and matches nothing the caller saw.
          Same fate, same line of code.

        Comparison is by exact text on a `/`-separated prefix, which is
        what `relative_path` holds -- `ImagePath` stores posix form, so
        there is no `\\` in the column to handle. The trailing separator
        on the prefix is load-bearing: without it `2018` would match
        `2018b`, which is a different folder (RFC-029 section 10).

        A subfolder with no indexed images is absent, not zero, for the
        reason `count_by_device()` omits an empty device: the names come
        from the caller, and inventing keys here would mean guessing
        them.
        """
