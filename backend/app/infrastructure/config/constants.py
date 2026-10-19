"""Project-wide constants for the configuration layer."""

SUPPORTED_IMAGE_EXTENSIONS: tuple[str, ...] = (
    ".jpg",
    ".jpeg",
    ".png",
    ".tiff",
    ".bmp",
    ".webp",
)

DEFAULT_VECTOR_DISTANCE: str = "cosine"
PROJECT_ENCODING: str = "utf-8"

EXIT_CANNOT_START: int = 78
"""The exit code of a program that cannot start, and that restarting will not fix.

RFC-036 section 6.1. The line on stderr says why, and is unchanged from RFC-034
section 5.5 and RFC-035 section 6.5; the code is what lets the launcher tell
"the configuration file is invalid" from "a defect brought the process down"
without reading the line. 78 is `EX_CONFIG` of `sysexits.h`. Windows gives the
number no meaning, and it collides with nothing Python or uvicorn already use:
1 for an unhandled exception, 2 for a bad argument, 3 for uvicorn's
`STARTUP_FAILURE`, and `0xC000013A` for a Ctrl+C nobody handled.
"""
