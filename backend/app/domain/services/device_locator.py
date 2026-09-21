"""Asking the filesystem where a device is, without importing the filesystem.

Three questions the Application must be able to ask -- *is this disk
plugged in right now?*, *is this folder actually on it?* and, since
RFC-031, *what folders are directly inside it?* -- and all three are
questions for the operating system. The adapter that answers them is
`WindowsVolumeIdentityProvider`, which is an Infrastructure ABC the
Application is forbidden to import
(`test_application_architecture.py` fails the build if it does), so the
dependency is inverted here instead.

*What is attached to this machine?* is deliberately **not** here. Every
signature below takes a `Device`, because every question below is about a
disk the system already knows; asking what is plugged in is asking about
volumes that are not a device yet, and it has its own port
(`VolumeCatalog`).

**Nothing may cache across calls.** A device's mount point is a fact about
this instant, and the whole of RFC-027 section 7 is that no stored value
can keep it -- the user pulls the cable and nothing notifies this process.
An implementation that remembered an answer would hand a job a drive
letter that now belongs to a different disk.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from app.domain.entities.device import Device
from app.domain.value_objects.job_scope import JobScope


@dataclass(frozen=True)
class FolderEntry:
    """One folder directly inside another, and whether it has any of its own.

    `has_children` travels with the name rather than being left for the
    caller to work out, because working it out means **opening the
    folder**: a listing of forty subfolders costs forty-one `readdir`
    calls, not one, and only the adapter can pay that without the
    Application touching a filesystem (RFC-031 section 4.4 of the build
    prompt).

    It is here at all because a UI that guessed would guess wrong in the
    visible direction: an expand arrow drawn on a leaf folder is an error
    the user only discovers by clicking it.
    """

    name: str
    """The folder's own name, as the filesystem spells it.

    A single path component, never a path. The caller joins it onto the
    canonical spelling of the parent, which is what makes the result
    something a client can send straight back as a job scope.
    """

    has_children: bool
    """Whether this folder contains at least one subfolder.

    `False` for a folder the platform refused to open -- a permission
    wall, a disk that went away mid-listing. An implementation logs that
    and carries on rather than failing the whole listing, which is the
    same rule RFC-027 applies to a volume that will not be named.
    """


class DeviceLocator(ABC):
    """Port for locating a device's files at the moment the question is asked."""

    @abstractmethod
    def mount_point(self, device: Device) -> Path | None:
        """Return where `device` is mounted right now, or `None` if nowhere.

        `None` is the answer for a disk in a drawer, and it is a first-
        class one rather than an error: the system knows the device
        perfectly well, it simply has no bytes to offer (RFC-027 section
        2.3). Callers turn it into `DeviceNotConnectedError` when they
        needed the bytes.

        The returned path is never persisted by anyone. It is valid until
        the user unplugs the disk, which is precisely why `Device` has no
        field for it.
        """

    @abstractmethod
    def resolve_scope(self, device: Device, scope: JobScope) -> JobScope | None:
        """Return `scope` as the filesystem spells it, or `None` if absent.

        Does two things that cannot be done above this layer, and both are
        why the port has this method rather than a plain `exists()`:

        * it confirms the scope resolves to a directory that is genuinely
          *inside* the device. `JobScope` has already refused `..`, a
          drive anchor and a UNC path, but an NTFS junction sitting inside
          the scope points somewhere else entirely and looks like an
          ordinary folder until it is resolved (RFC-029 section 7.1);
        * it returns the folder's canonical spelling. Windows paths are
          case-insensitive, so `Fotos` and `fotos` are one directory --
          and `normalize_scopes()` compares parts exactly, so without
          canonicalisation here two spellings of the same folder would
          both survive normalisation and be walked twice.

        `None` means the scope is not a directory on this device, which
        the caller reports as an invalid scope. A device that is not
        mounted at all answers `None` for every scope; callers check the
        connection first so that the user is told which disk to plug in
        rather than that their folder is wrong.
        """

    @abstractmethod
    def list_folders(self, device: Device, scope: JobScope) -> list[FolderEntry] | None:
        """Return the folders directly inside `scope`, or `None` if it is absent.

        **One level, and no files.** The caller is drawing one step of a
        tree the user is walking, so the cost follows what they opened
        rather than the size of the disk (RFC-031 section 8.1). Files are
        not returned at all: what this feeds is a folder picker whose
        output goes back as `scopes[]`, and a file is not a scope.

        **Nothing counts anything here.** How many images a folder has
        indexed is a fact about our own table, answered by
        `ImageRepository`; how many files are on the disk under it is the
        subtree walk RFC-029 section 7.2 measured as the dominant cost on
        a cold mechanical disk, and RFC-031 section 2.3 refuses to pay it
        twice.

        `None` carries exactly the meaning it carries in
        `resolve_scope()`: this is not a folder on this device -- absent,
        a file, or outside the device once NTFS junctions are resolved. A
        device that is not mounted answers `None` for everything, and
        callers check the connection first so the user is told which disk
        to plug in.

        Order is not part of the contract, because the platform's own is
        not: `scandir` returns entries in whatever order the filesystem
        keeps them. A caller that renders the list sorts it.
        """
