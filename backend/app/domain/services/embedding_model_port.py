"""Abstract contract for components that generate embeddings from domain objects."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.domain.entities.image import Image
from app.domain.value_objects.embedding_vector import EmbeddingVector


class EmbeddingModelPort(ABC):
    """Generate embeddings from domain data without mutating the supplied inputs.

    Implementations must treat the provided domain objects as read-only and must not
    mutate the supplied Image instance or the text string.
    """

    @abstractmethod
    def encode_image(self, image: Image) -> EmbeddingVector:
        """Return an embedding for the supplied image."""

    @abstractmethod
    def encode_text(self, text: str) -> EmbeddingVector:
        """Return an embedding for the supplied text."""

    def encode_images(self, images: Sequence[Image]) -> list[EmbeddingVector]:
        """Return one embedding per supplied image, in the order given.

        Concrete rather than abstract, and that is the whole design. An
        implementation that has a faster way to encode several images at
        once overrides this; one that does not inherits the loop below and
        stays correct without writing a second copy of anything. Adding
        another required method would have forced every implementation --
        including the test double -- to grow code for an optimization it
        does not have.

        The alternative RFC-024 considered was a separate optional
        contract that callers probe for at runtime. That pushes an
        `isinstance` check into the Application layer, which is a type
        test standing in for a contract; the default implementation here
        gives the same "implementations may ignore this" property with no
        probe and no branch.

        The result must equal `[self.encode_image(image) for image in
        images]` to within floating-point tolerance. Encoding several
        images together may change *how* the work is scheduled; it must
        never change *what* is computed.

        Errors propagate. A failure anywhere in the group aborts the whole
        call, and it is the caller's job -- not this method's -- to work
        out which image was responsible, because only the caller knows
        whether retrying one at a time is worth the time it costs.
        """
        return [self.encode_image(image) for image in images]
