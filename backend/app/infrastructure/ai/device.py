"""Central inference-device resolution for the AI infrastructure adapters."""

from __future__ import annotations

import torch

from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

AUTO_DEVICE = "auto"


def resolve_device(configured: str | None = None) -> torch.device:
    """Resolve the torch device every AI adapter in this package should use.

    Resolved in exactly one place so device selection never gets scattered
    across adapters (RFC-023 section 9). `configured` defaults to
    `settings.device`, whose own default is `auto`:

    - `auto`  -- CUDA when torch reports it available, CPU otherwise, so a
      plain CPU-only development machine stays fully usable;
    - `cuda`  -- honoured when available, and degraded to CPU with a warning
      when it is not. CUDA is an optimization here, never a requirement, so
      an unsatisfiable request must not take the application down;
    - anything else is passed to `torch.device` verbatim.

    GPU backends other than CUDA (ROCm, MPS, XPU) are out of scope for
    RFC-023 and are neither detected nor special-cased.
    """
    raw = configured if configured is not None else settings.device
    requested = raw.strip().lower()

    if requested == AUTO_DEVICE:
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")

    if requested.startswith("cuda") and not torch.cuda.is_available():
        logger.warning(
            "Device %r was requested but torch reports no CUDA device; using CPU",
            requested,
        )
        return torch.device("cpu")

    return torch.device(requested)
