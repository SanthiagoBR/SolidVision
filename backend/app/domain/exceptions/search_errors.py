"""Search-specific domain exceptions (RFC-025)."""

from app.domain.exceptions.domain_error import DomainError


class EmptySearchQueryError(DomainError):
    """Raised when a search query carries no searchable text.

    A blank query is rejected rather than answered. There is no sensible
    ranking of every image against nothing, and the embedding model would
    happily encode `""` into a vector that ranks the corpus by an
    accident of the text tower.
    """

    def __init__(self, message: str = "Search query cannot be empty.") -> None:
        super().__init__(message)


class InvalidSearchLimitError(DomainError):
    """Raised when the requested number of results is outside the allowed range.

    Both ends raise rather than clamp. Silently returning 100 results to a
    caller that asked for 10,000 -- or 1 to a caller that asked for 0 --
    hides the disagreement in data the caller then draws conclusions from,
    where an exception puts it in front of whoever wrote the call.
    """

    def __init__(self, message: str = "Invalid search limit.") -> None:
        super().__init__(message)


class EmbeddingDimensionMismatchError(DomainError):
    """Raised when a query vector does not match the indexed embedding space.

    Comparing vectors of different widths is meaningless, not empty. The
    distinction matters because the plausible failure is silent: Python's
    `zip` truncates to the shorter operand, so a hand-rolled cosine loop
    over a 3-dimensional query and 512-dimensional rows returns a
    perfectly reasonable-looking number computed from three of the 512
    dimensions. Every repository implementation raises this instead, so
    that a wrong-sized vector fails the same way in memory as it does
    against a `vector(512)` column.
    """

    def __init__(
        self, message: str = "Embedding dimension does not match the index."
    ) -> None:
        super().__init__(message)


class UnreadableQueryImageError(DomainError):
    """Raised when a picture sent as a query cannot be decoded (RFC-038 4.2).

    Bytes that are not an image, a JPEG cut short, a format other than the one
    declared, or more pixels than the decoder agrees to open. Refused before
    the model sees it: the adapter lets its decoding errors through on
    purpose, and one reaching it would be a 500 for what is the caller's file.
    """

    def __init__(self, message: str = "The query image cannot be read.") -> None:
        super().__init__(message)
