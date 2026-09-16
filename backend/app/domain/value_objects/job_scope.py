"""The folder an indexing job is restricted to, and how several combine.

A scope is a path *relative to a device's mount point*, which is the only
form that survives the disk being unplugged and coming back as another
drive letter (RFC-027 section 4). Everything here exists to keep it that
way: an absolute path, a drive anchor, a UNC share and a `..` component
are all refused at construction rather than sanitised, because each of
them is a request to index something outside the device the job names.

**Scopes are held as `parts`, not as a string.** Ordering and containment
are the two operations this module performs, and both are tuple
operations on parts: `("2018",)` contains `("2018", "junho")` because it
is a prefix of it. Doing the same on text would make `2018` look like a
prefix of `2018b`, which is a different folder (RFC-029 section 10, and
section 4.4 of the RFC-029 build prompt).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath, PureWindowsPath

from app.domain.exceptions import InvalidJobScopeError


@dataclass(frozen=True)
class JobScope:
    """One folder within a device, as a device-relative path.

    The empty scope -- no parts at all -- means *the whole device*, which
    is what RFC-029 section 5.2 stores as zero scope rows. `""` and `"."`
    both normalise to it, so a client that sends either gets the whole
    disk and is told so by the normalised scopes echoed back in the 202.
    """

    parts: tuple[str, ...]

    def __init__(self, value: str | PurePath | tuple[str, ...] = ()) -> None:
        object.__setattr__(self, "parts", self._normalize(value))

    @staticmethod
    def _normalize(value: str | PurePath | tuple[str, ...]) -> tuple[str, ...]:
        """Validate and split a scope, refusing everything that escapes.

        The checks run in this order because the order is the guarantee.
        `..` is rejected **before** anything is resolved: resolving first
        and checking afterwards is how a scope that escapes gets one
        chance to be read as a real path, and it also means the refusal
        would depend on the folder existing. A scope that escapes is
        invalid whether or not the disk is plugged in.

        Both path flavours are consulted for the anchor test. Windows
        alone would miss nothing today, but POSIX is what catches a bare
        leading `/` in a way that does not depend on the platform running
        this process -- and the Domain has no business knowing which one
        that is (RFC-029 section 4.1 of the build prompt: nothing outside
        the volume adapter depends on Windows).
        """
        if isinstance(value, tuple):
            text = "/".join(value)
        elif isinstance(value, PurePath):
            text = str(value)
        elif isinstance(value, str):
            text = value
        else:
            raise InvalidJobScopeError(
                "Scope must be a string, a PurePath, or a tuple of parts"
            )

        # Separators are normalised first so that the anchor tests below
        # see the same string the caller meant. A Windows client sends
        # `2018\junho`; refusing it for "containing a backslash" would be
        # pedantry, while reading it as a single part named `2018\junho`
        # would be worse -- it would pass every check and match nothing.
        text = text.replace("\\", "/").strip()

        if PureWindowsPath(text).anchor or PurePosixPath(text).anchor:
            raise InvalidJobScopeError(
                f"Scope must be relative to the device, got {text!r}. A drive "
                "letter, a leading separator or a UNC share names a location "
                "outside the device this job is for."
            )

        parts = PurePosixPath(text).parts
        if any(part == ".." for part in parts):
            raise InvalidJobScopeError(
                f"Scope must not contain '..', got {text!r}. A scope that "
                "walks out of the device is not a scope of that device."
            )

        # `PurePosixPath` has already dropped `.` and collapsed repeated
        # separators, so what is left is either the real parts or nothing
        # at all -- and nothing at all is the whole device, not an error.
        return parts

    def __str__(self) -> str:
        """Render as the device-relative path, `""` for the whole device."""
        return "/".join(self.parts)

    @property
    def is_whole_device(self) -> bool:
        """Whether this scope covers everything on the device."""
        return not self.parts

    def contains(self, other: JobScope) -> bool:
        """Whether every file under `other` is also under this scope.

        True for a scope and itself, which is what makes the absorption in
        `normalize_scopes()` idempotent. The whole-device scope contains
        everything, including itself, by the same prefix rule -- an empty
        tuple is a prefix of every tuple.
        """
        return other.parts[: len(self.parts)] == self.parts


def normalize_scopes(scopes: tuple[JobScope, ...]) -> tuple[JobScope, ...]:
    """Absorb contained scopes and return what will actually be walked.

    `["2018/", "2018/junho"]` becomes `["2018"]`: the second is inside the
    first, and walking both would discover every file under `2018/junho`
    twice -- paying for two `stat`s and two EXIF reads per file, and, far
    worse, producing a discovery order in which a path appears twice. The
    checkpoint of RFC-029 section 10 is a *position* in that order, and a
    position is only meaningful in a sequence that visits each file once.

    The result is what the 202 echoes back, so a client that asked for two
    overlapping folders can see the one job it actually got.

    **Order is not the discovery order**, and must not be mistaken for it.
    The parts tuples are sorted here so the result is deterministic and
    stable to compare in a test, but the sequence the scan walks is
    established by `FilesystemImageProvider`, which sorts with the running
    platform's own path ordering -- case-insensitively on Windows, where
    `a b` sorts before `B`. Sorting here with a rule the filesystem does
    not share would put the boundary between two scopes in a different
    place than the scan does, and the resume comparison spans those
    boundaries.

    Duplicates fall out for free: a scope contains itself, so the second
    copy is absorbed by the first.
    """
    if any(scope.is_whole_device for scope in scopes):
        # Zero rows already means "the whole device" (RFC-029 section
        # 5.2), so an explicit whole-device scope collapses to that form
        # rather than being stored as a row meaning the same thing. It
        # absorbs every other scope on the way.
        return ()

    ordered = sorted(set(scopes), key=lambda scope: scope.parts)
    kept: list[JobScope] = []
    for scope in ordered:
        # Sorted by parts, so any scope that contains `scope` is already
        # in `kept` and is the last one that could: a container is a
        # prefix, and a prefix sorts before what it contains.
        if kept and kept[-1].contains(scope):
            continue
        kept.append(scope)
    return tuple(kept)
