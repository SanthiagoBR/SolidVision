"""File-access domain exceptions (RFC-030).

RFC-030 section 11 lists this module as the home of two errors, "device
disconnected" and "file missing", and only one of them is new. The first
already existed as `DeviceNotConnectedError`, raised by
`Image.require_absolute_path()` and mapped to 409 since RFC-029; a second
class for the same fact would be two names the HTTP layer had to agree
about, so `/reveal` raises the existing one.
"""

from app.domain.exceptions.domain_error import GoneError, NotFoundError


class FileGoneError(GoneError):
    """Raised when an indexed file is missing although its disk is connected.

    The 410 of RFC-030 section 5.1: the row exists, the device is mounted
    right now, and there is nothing at the path the row describes -- the
    user deleted, moved or renamed the file after it was indexed.

    Deliberately not raised when the *device* is missing. That is
    `DeviceNotConnectedError`, and the two call for opposite reactions:
    "plug in HD3" is advice that works, while for a deleted file it would
    send the user to fetch a disk that does not have the photo on it.

    Nothing drops the row when this is raised. Deciding that a missing
    file means a stale row is a reconciliation question, and answering it
    from inside a request to open a folder would make clicking "show in
    Explorer" a destructive action.
    """

    def __init__(self, message: str = "File is no longer on its device.") -> None:
        super().__init__(message)


class ThumbnailNotFoundError(NotFoundError):
    """Raised when an image exists but has no servable thumbnail.

    A 404 in every variant, and the reason it is not a `GoneError` is who
    owns the missing file. RFC-030 section 7.2 has the UI show a
    placeholder on 404, which covers the ordinary cases -- an image indexed
    before RFC-030, or one whose thumbnail failed to render. It also covers
    a row that names a thumbnail the cache directory no longer holds:
    that cache belongs to this application, so its absence is a cleanup
    or a bug on this side, not the world changing under the caller. 410 is
    reserved for the user's own file, which is the case `/reveal` exists
    to report.
    """

    def __init__(self, message: str = "Thumbnail not found.") -> None:
        super().__init__(message)
