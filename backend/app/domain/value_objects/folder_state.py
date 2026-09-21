"""What a folder's indexing looks like from the outside (RFC-031 section 8.2).

Five values, derived per request from the job history and the indexed
count. Nothing here is stored: a folder is not a row, and the state is a
reading of `indexing_jobs` and `images` taken at the moment somebody
asked.

**There is no percentage, and there is no room for one.** RFC-031 section
2.3: a per-folder percentage would need a denominator -- how many
supported files are on the disk under this folder -- and obtaining it
means walking the subtree, which is the read RFC-029 section 7.2 measured
as dominant on a cold mechanical disk. `discovered_files` is a rising
counter for exactly that reason and is not a total. So the API reports
`indexed_images`, which is an exact fact about our own table, beside one
of these five words. *"1,204 indexed"* is true; *"30% indexed"* has
nothing to be 30% of.

The enum lives in the Domain although the derivation lives in the
Application, and the split is deliberate: *which states exist* -- that
`partial` is a thing a folder can be, and that it is not a failure -- is a
rule about the product. *Reading the job list to pick one* composes two
repositories and a locator, which is what a use case is for
(`AI_Context.md`).
"""

from __future__ import annotations

import enum


class FolderState(enum.StrEnum):
    """How much of a folder this system has indexed, as one word.

    A `StrEnum` so the member *is* the string on the wire, following
    `JobStatus`. Unlike `JobStatus` it is never written to a column --
    there is no folder table and RFC-031 adds no migration -- so the
    choice is only about the JSON.
    """

    INDEXING = "indexing"
    """A `running` job's scopes cover this folder."""

    QUEUED = "queued"
    """A `pending` job's scopes cover this folder; nothing is moving yet."""

    PARTIAL = "partial"
    """Some of it has been indexed, and nothing claims all of it has.

    Three situations land here, and they are one situation seen three
    ways -- *we have rows under this folder and no completed job that
    covers the whole of it*:

    * the most recent covering job ended `cancelled` or `failed`. Its
      `last_processed_relative_path` travels in the response, because
      *where it stopped* is what a user can act on;
    * a job completed, but its scope was **inside** this folder rather
      than over it -- indexing `2018/janeiro/casamento` leaves
      `2018/janeiro` partly done, which is literally what this word says;
    * images exist under the folder and no job in the history covers it
      at all. That is a disk indexed before jobs existed, or one whose
      history has been trimmed. `never_indexed` would be a false
      statement about rows that are demonstrably there.
    """

    INDEXED = "indexed"
    """The most recent job covering the whole folder ended `completed`."""

    NEVER_INDEXED = "never_indexed"
    """No job ever covered it **and** it has no indexed images.

    Both halves are required. A folder with rows and no job is `partial`,
    not this: saying "never indexed" about images the search can already
    return is the kind of confidently wrong statement RFC-031 section 4.2
    exists to keep out of the API.
    """
