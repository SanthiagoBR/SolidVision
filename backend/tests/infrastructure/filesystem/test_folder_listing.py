r"""`MountedDeviceLocator.list_folders()` over a real directory tree (RFC-031 §8).

The adapter is where `has_children` is actually paid for and where the
containment check actually runs, so both are tested here against real
directories rather than through a double. `WindowsVolumeIdentityProvider`
is stubbed out -- these are questions about `os.scandir` and
`Path.resolve`, not about `kernel32`, which has its own file in
`test_volume_identity.py`.

The escape tests matter most. `JobScope` refuses `..`, a drive anchor and
a UNC share without touching the disk, and this layer is what catches the
one it cannot see: an NTFS junction inside the scope, which looks like an
ordinary folder until it is resolved.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from tests.application.fakes import StubVolumeIdentityProvider
from tests.conftest import TEST_VOLUME_IDENTITY, make_test_device

from app.domain.entities.device import Device
from app.domain.value_objects.job_scope import JobScope
from app.infrastructure.filesystem.mounted_device_locator import MountedDeviceLocator


@pytest.fixture()
def disk(tmp_path: Path) -> Path:
    """A mount point with a folder that has children and two that do not."""
    (tmp_path / "2018" / "janeiro" / "casamento").mkdir(parents=True)
    (tmp_path / "2018" / "fevereiro").mkdir()
    (tmp_path / "2018" / "junho").mkdir()
    (tmp_path / "2018" / "loose.jpg").write_bytes(b"\xff\xd8")
    (tmp_path / "2018" / "fevereiro" / "photo.jpg").write_bytes(b"\xff\xd8")
    return tmp_path


@pytest.fixture()
def locator(disk: Path) -> MountedDeviceLocator:
    return MountedDeviceLocator(
        StubVolumeIdentityProvider(identity=TEST_VOLUME_IDENTITY, mount_point=disk)
    )


@pytest.fixture()
def device() -> Device:
    return make_test_device("HD3")


def names(locator: MountedDeviceLocator, device: Device, path: str) -> list[str]:
    entries = locator.list_folders(device, JobScope(path))
    assert entries is not None
    return sorted(entry.name for entry in entries)


class TestWhatItLists:
    def test_it_lists_folders_one_level_down(
        self, locator: MountedDeviceLocator, device: Device
    ) -> None:
        assert names(locator, device, "2018") == ["fevereiro", "janeiro", "junho"]

    def test_it_lists_the_device_root_for_an_empty_scope(
        self, locator: MountedDeviceLocator, device: Device
    ) -> None:
        assert names(locator, device, "") == ["2018"]

    def test_files_are_not_listed(
        self, locator: MountedDeviceLocator, device: Device
    ) -> None:
        """What goes back in `scopes[]` is a folder; a file is not one."""
        assert "loose.jpg" not in names(locator, device, "2018")

    def test_it_does_not_recurse(
        self, locator: MountedDeviceLocator, device: Device
    ) -> None:
        """One level per request. `casamento` is two levels down from `2018`."""
        assert "casamento" not in names(locator, device, "2018")

    def test_an_empty_folder_lists_nothing_rather_than_answering_none(
        self, locator: MountedDeviceLocator, device: Device
    ) -> None:
        """`None` means "not a folder of this device"; empty is a real answer."""
        assert locator.list_folders(device, JobScope("2018/junho")) == []


class TestHasChildren:
    def test_a_folder_with_a_subfolder_reports_true(
        self, locator: MountedDeviceLocator, device: Device
    ) -> None:
        entries = locator.list_folders(device, JobScope("2018"))
        assert entries is not None

        by_name = {entry.name: entry.has_children for entry in entries}
        assert by_name["janeiro"] is True

    def test_a_folder_holding_only_files_reports_false(
        self, locator: MountedDeviceLocator, device: Device
    ) -> None:
        """The distinction the UI draws an expand arrow from.

        `fevereiro` has a photo in it and no folders, so it is a leaf for
        this purpose -- and a locator that answered "is it empty" instead
        of "does it hold folders" would get exactly this case wrong.
        """
        entries = locator.list_folders(device, JobScope("2018"))
        assert entries is not None

        by_name = {entry.name: entry.has_children for entry in entries}
        assert by_name["fevereiro"] is False
        assert by_name["junho"] is False


class TestWhatItRefuses:
    def test_an_absent_folder_answers_none(
        self, locator: MountedDeviceLocator, device: Device
    ) -> None:
        assert locator.list_folders(device, JobScope("1999")) is None

    def test_a_file_answers_none(
        self, locator: MountedDeviceLocator, device: Device
    ) -> None:
        assert locator.list_folders(device, JobScope("2018/loose.jpg")) is None

    def test_a_disconnected_device_answers_none_for_everything(
        self, device: Device
    ) -> None:
        offline = MountedDeviceLocator(
            StubVolumeIdentityProvider(identity=TEST_VOLUME_IDENTITY, mount_point=None)
        )

        assert offline.list_folders(device, JobScope("2018")) is None
        assert offline.list_folders(device, JobScope("")) is None

    @pytest.mark.parametrize("path", ["..", "../..", "2018/../..", "C:\\", "//srv/x"])
    def test_an_escaping_scope_never_reaches_this_layer(self, path: str) -> None:
        """`JobScope` refuses these at construction, before any disk is touched.

        Asserted here rather than only in `test_job_scope.py` because the
        ordering is the guarantee: a scope that escapes must be refused
        whether or not the folder exists, and must never get one chance
        to be resolved as a real path.
        """
        from app.domain.exceptions import InvalidJobScopeError

        with pytest.raises(InvalidJobScopeError):
            JobScope(path)


@pytest.mark.skipif(
    subprocess.run(  # noqa: S603
        ["cmd", "/c", "ver"], capture_output=True, check=False
    ).returncode
    != 0,
    reason="needs cmd.exe to create an NTFS junction",
)
class TestAJunctionCannotEscapeTheDevice:
    """The escape `JobScope` cannot see, and the reason `_locate()` is shared.

    A junction inside the scope points at another volume entirely and is
    indistinguishable from an ordinary folder until it is resolved. It is
    the case RFC-029 section 7.1 added `resolve_scope()`'s containment
    check for, and `list_folders()` goes through the same helper so that
    a listing cannot escape where a job could not.
    """

    def test_a_junction_pointing_outside_the_device_is_refused(
        self, tmp_path: Path, device: Device
    ) -> None:
        mount = tmp_path / "disk"
        (mount / "fotos").mkdir(parents=True)
        outside = tmp_path / "elsewhere"
        (outside / "secrets").mkdir(parents=True)

        created = subprocess.run(  # noqa: S603
            ["cmd", "/c", "mklink", "/J", str(mount / "escape"), str(outside)],
            capture_output=True,
            check=False,
        )
        if created.returncode != 0:
            pytest.skip("this filesystem would not create a junction")

        locator = MountedDeviceLocator(
            StubVolumeIdentityProvider(identity=TEST_VOLUME_IDENTITY, mount_point=mount)
        )

        assert locator.list_folders(device, JobScope("escape")) is None
        assert locator.resolve_scope(device, JobScope("escape")) is None
