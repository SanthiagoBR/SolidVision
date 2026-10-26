"""The checks of the package refuse what they exist to refuse (RFC-041 7.3).

`scripts/check_package.py` fails the build, so what it fails on is tested
here, on small trees: the path budget, with the 75 characters of the longest
profile and the zip that only a `licenses` tree of a `dist-info` gets; the
DLLs, over a `System32` of make-believe where an API set passes, a protected
DLL passes, and one that only that `System32` has, unprotected, fails; the
`app.asar` listing; and `resources` against the montagem. The real package is
checked by `npm run dist` (section 9).
"""

from __future__ import annotations

import importlib.util
import json
import struct
import sys
import zipfile
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.pe_files import make_pe

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def load(name: str) -> ModuleType:
    """A script, loaded from its file: `scripts/` is not a package."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def check() -> ModuleType:
    return load("check_package")


@pytest.fixture(scope="module")
def pe() -> ModuleType:
    return load("pe_imports")


def write(path: Path, content: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# The path budget (section 2.5)
# --------------------------------------------------------------------------


def test_the_budget_is_the_longest_profile_of_section_2_5(check: ModuleType) -> None:
    """A local account name has at most 20 characters: the installation folder
    is then 65 characters long, `resources\\` 75, and `MAX_PATH` leaves 259."""
    assert len(check.INSTALL_PREFIX) == 65
    assert len(check.RESOURCES_PREFIX) == 75
    assert check.RESOURCES_PREFIX.startswith(check.INSTALL_PREFIX)
    assert check.MAX_PATH_CHARS == 259


def prefix_leaving(characters: int, check: ModuleType) -> str:
    """A prefix that leaves `characters` for the relative path: the files on
    disk stay short, whatever this machine allows."""
    budget: int = check.MAX_PATH_CHARS
    return "C:\\" + "p" * (budget - characters - 3)


def test_a_file_at_the_budget_passes_and_one_character_more_fails(
    check: ModuleType, tmp_path: Path
) -> None:
    prefix = prefix_leaving(20, check)
    write(tmp_path / ("a" * 20))
    write(tmp_path / ("b" * 21))

    over = check.over_budget(tmp_path, prefix)

    assert over == [(check.MAX_PATH_CHARS + 1, "b" * 21)]


def test_the_relative_path_is_counted_with_windows_separators(
    check: ModuleType, tmp_path: Path
) -> None:
    write(tmp_path / "ab" / "cd")

    assert check.longest_path(tmp_path, "") == (5, "ab\\cd")


def test_a_licenses_tree_of_a_dist_info_over_the_budget_becomes_a_zip(
    check: ModuleType, tmp_path: Path
) -> None:
    """The torch case of section 2.5: the notice stays in the package, inside
    `licenses.zip`, with the path it had."""
    dist_info = tmp_path / "Lib" / "site-packages" / "torch-2.13.0+cpu.dist-info"
    deep = dist_info / "licenses" / "third_party" / "kineto" / "LICENSE.txt"
    write(deep, "the notice")
    write(dist_info / "licenses" / "LICENSE", "short")
    write(dist_info / "METADATA")
    prefix = prefix_leaving(len(check.windows_relative(deep, tmp_path)) - 1, check)

    zipped = check.zip_license_trees(tmp_path, prefix)

    assert zipped == [r"Lib\site-packages\torch-2.13.0+cpu.dist-info\licenses.zip"]
    assert not (dist_info / "licenses").exists()
    with zipfile.ZipFile(dist_info / "licenses.zip") as archive:
        assert sorted(archive.namelist()) == [
            "LICENSE",
            "third_party/kineto/LICENSE.txt",
        ]
        assert archive.read("third_party/kineto/LICENSE.txt") == b"the notice"
    assert (dist_info / "METADATA").is_file()
    check.check_path_budget(tmp_path, prefix, check.Report(), "the test")


def test_any_other_file_over_the_budget_is_left_and_fails_the_build(
    check: ModuleType, tmp_path: Path
) -> None:
    """Only a `licenses` tree of a `dist-info` is zipped: a module, or a
    `licenses` folder of a package, is something the runtime may read."""
    package = write(tmp_path / "pkg" / "licenses" / "deep" / "module.py")
    metadata = write(tmp_path / "pkg.dist-info" / "other" / "deep" / "NOTICE")
    longest = max(
        len(check.windows_relative(path, tmp_path)) for path in (package, metadata)
    )
    prefix = prefix_leaving(longest - 1, check)

    assert check.zip_license_trees(tmp_path, prefix) == []
    with pytest.raises(check.PackageError, match="pass 259 characters"):
        check.check_path_budget(tmp_path, prefix, check.Report(), "the test")

    assert package.is_file() and metadata.is_file()


def test_a_tree_within_the_budget_is_left_alone(
    check: ModuleType, tmp_path: Path
) -> None:
    write(tmp_path / "x.dist-info" / "licenses" / "LICENSE")

    assert check.zip_license_trees(tmp_path, check.RESOURCES_PREFIX) == []
    assert (tmp_path / "x.dist-info" / "licenses" / "LICENSE").is_file()


# --------------------------------------------------------------------------
# The DLLs (section 2.3)
# --------------------------------------------------------------------------


def fake_windows(
    pe: ModuleType,
    tmp_path: Path,
    protected: Iterable[str],
    unprotected: Iterable[str] = (),
    known: Iterable[str] = (),
) -> Any:
    """A `System32` of make-believe, and a resource protection that says yes
    to the names it is told."""
    system32 = tmp_path / "Windows" / "System32"
    shielded = set(protected)
    for name in shielded | set(unprotected):
        make_pe(system32 / name, ["kernel32.dll"])

    def protects(path: Path) -> bool:
        return path.name.lower() in shielded

    return pe.Windows(system32, frozenset(known), protects)


def test_an_api_set_a_known_dll_and_a_protected_dll_are_windows_own(
    pe: ModuleType, tmp_path: Path
) -> None:
    tree = tmp_path / "python"
    make_pe(
        tree / "python.exe",
        ["api-ms-win-crt-runtime-l1-1-0.dll", "KERNEL32.dll", "ucrtbase.dll"],
        delayed=["ext-ms-win-ntuser-window-l1-1-0.dll"],
    )
    windows = fake_windows(
        pe, tmp_path, protected={"ucrtbase.dll"}, known={"kernel32.dll"}
    )

    report = pe.resolve_scope(tree, windows)

    assert report.missing == []
    assert report.from_windows == {
        "api-ms-win-crt-runtime-l1-1-0.dll": "api set",
        "ext-ms-win-ntuser-window-l1-1-0.dll": "api set",
        "kernel32.dll": "KnownDLLs",
        "ucrtbase.dll": "System32, protected",
    }


def test_a_dll_only_this_machines_system32_has_unprotected_is_missing(
    pe: ModuleType, tmp_path: Path
) -> None:
    """The case of section 2.3: `msvcp140.dll` from a redistributable passes
    every test on the machine that has it, and fails on the next one."""
    tree = tmp_path / "python"
    make_pe(
        tree / "Lib" / "site-packages" / "torch" / "lib" / "torch_cpu.dll",
        ["MSVCP140.dll"],
    )
    windows = fake_windows(pe, tmp_path, protected=set(), unprotected={"msvcp140.dll"})

    report = pe.resolve_scope(tree, windows)

    assert [missing.name for missing in report.missing] == ["msvcp140.dll"]
    assert report.missing[0].unprotected_in_system32 is True
    assert report.missing[0].wanted_by == ["Lib/site-packages/torch/lib/torch_cpu.dll"]
    assert "only in this machine's System32" in report.missing[0].line()


def test_a_name_the_scope_has_is_found_in_it(pe: ModuleType, tmp_path: Path) -> None:
    tree = tmp_path / "python"
    make_pe(tree / "python.exe", ["python312.dll"])
    make_pe(tree / "python312.dll", ["kernel32.dll"])
    make_pe(
        tree / "Lib" / "site-packages" / "torch" / "lib" / "c10.dll", ["kernel32.dll"]
    )
    make_pe(tree / "Lib" / "site-packages" / "torch" / "_C.pyd", ["c10.dll"])
    windows = fake_windows(pe, tmp_path, protected=set(), known={"kernel32.dll"})

    report = pe.resolve_scope(tree, windows)

    assert report.missing == []
    assert report.from_scope == {
        "c10.dll": "Lib/site-packages/torch/lib/c10.dll",
        "python312.dll": "python312.dll",
    }


def test_another_runtime_does_not_provide_a_name(
    pe: ModuleType, tmp_path: Path
) -> None:
    """`runtime\\postgres` has an `msvcp140.dll` no Python process looks for."""
    resources = tmp_path / "resources"
    make_pe(resources / "SolidVision.exe", ["kernel32.dll"])
    make_pe(resources / "runtime" / "postgres" / "msvcp140.dll", ["kernel32.dll"])
    python = resources / "runtime" / "python"
    make_pe(python / "python.exe", ["msvcp140.dll"])
    windows = fake_windows(pe, tmp_path, protected=set(), known={"kernel32.dll"})

    assert pe.missing_names(python, windows) == ["msvcp140.dll"]
    assert pe.missing_names(resources, windows, excluded=[python]) == []
    assert pe.pe_files(
        resources, excluded=[python, resources / "runtime" / "postgres"]
    ) == [resources / "SolidVision.exe"]


def test_a_name_loaded_by_ctypes_enters_the_check(
    pe: ModuleType, tmp_path: Path
) -> None:
    """`torch` loads three DLLs with `ctypes.CDLL`, which no table shows."""
    tree = tmp_path / "python"
    make_pe(tree / "python.exe", ["kernel32.dll"])
    windows = fake_windows(pe, tmp_path, protected=set(), known={"kernel32.dll"})

    report = pe.resolve_scope(tree, windows, by_name=["msvcp140.dll"])

    assert [(m.name, m.wanted_by) for m in report.missing] == [
        ("msvcp140.dll", ["ctypes"])
    ]


def test_a_file_named_like_a_binary_that_is_not_one_is_skipped(
    pe: ModuleType, tmp_path: Path
) -> None:
    tree = tmp_path / "python"
    write(tree / "Lib" / "site-packages" / "fixture.dll", "not a binary")
    windows = fake_windows(pe, tmp_path, protected=set())

    assert pe.resolve_scope(tree, windows).missing == []


def test_the_check_names_every_missing_dll_and_fails(
    check: ModuleType, pe: ModuleType, tmp_path: Path
) -> None:
    python = tmp_path / "runtime" / "python"
    make_pe(python / "python.exe", ["msvcp140.dll", "nowhere.dll"])
    windows = fake_windows(pe, tmp_path, protected=set(), unprotected={"msvcp140.dll"})

    with pytest.raises(check.PackageError) as raised:
        check.check_dlls([check.Scope("python", python)], check.Report(), windows)

    assert "python: msvcp140.dll: only in this machine's System32" in str(raised.value)
    assert "python: nowhere.dll: nowhere" in str(raised.value)


# --------------------------------------------------------------------------
# The folder of electron-builder
# --------------------------------------------------------------------------


def make_asar(path: Path, tree: dict[str, Any]) -> Path:
    """An `app.asar` header: two 32-bit sizes, then the JSON in a pickle."""
    text = json.dumps({"files": tree}).encode("utf-8")
    padded = text + bytes(-len(text) % 4)
    pickle = struct.pack("<II", len(padded) + 4, len(text)) + padded
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<II", 4, len(pickle)) + pickle)
    return path


SHELL = {
    "dist-electron": {"files": {"main.js": {"size": 1}, "preload.js": {"size": 1}}},
    "electron": {"files": {"startup.html": {"size": 1}}},
    "package.json": {"size": 1},
}


def test_the_asar_listing_is_read_from_its_header(
    check: ModuleType, tmp_path: Path
) -> None:
    archive = make_asar(tmp_path / "app.asar", SHELL)

    assert check.asar_files(archive) == [
        "dist-electron/main.js",
        "dist-electron/preload.js",
        "electron/startup.html",
        "package.json",
    ]


def test_an_asar_with_the_shell_only_passes(check: ModuleType, tmp_path: Path) -> None:
    make_asar(tmp_path / "resources" / "app.asar", SHELL)
    report = check.Report()

    check.check_asar(tmp_path, report)

    assert report.facts[0].startswith("app.asar: 4 files")


def test_an_asar_with_node_modules_fails(check: ModuleType, tmp_path: Path) -> None:
    """The page's dependencies are in the interface's build (section 7.1)."""
    tree = {
        **SHELL,
        "node_modules": {"files": {"react": {"files": {"index.js": {"size": 1}}}}},
    }
    make_asar(tmp_path / "resources" / "app.asar", tree)

    with pytest.raises(check.PackageError, match="node_modules/react/index.js"):
        check.check_asar(tmp_path, check.Report())


def test_resources_is_the_montagem_without_its_marker(
    check: ModuleType, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    unpacked = tmp_path / "win-unpacked"
    for root in (staging, unpacked / "resources"):
        write(root / "backend" / "app" / "__init__.py")
        write(root / "runtime" / "python" / "python.exe")
    write(staging / check.STAGED_MARKER, "{}")
    make_asar(unpacked / "resources" / "app.asar", SHELL)
    report = check.Report()

    check.check_same_resources(staging, unpacked, report)

    assert report.facts == [
        f"resources: the montagem's 2 files, 0.00 GB, without {check.STAGED_MARKER}"
    ]


@pytest.mark.parametrize("problem", ["marker", "missing", "extra"])
def test_resources_that_differ_from_the_montagem_fail(
    check: ModuleType, tmp_path: Path, problem: str
) -> None:
    staging = tmp_path / "staging"
    resources = tmp_path / "win-unpacked" / "resources"
    for root in (staging, resources):
        write(root / "backend" / "app" / "__init__.py")
    write(staging / check.STAGED_MARKER, "{}")
    if problem == "marker":
        write(resources / check.STAGED_MARKER, "{}")
    elif problem == "missing":
        write(staging / "frontend" / "dist" / "index.html")
    else:
        write(resources / "elsewhere.txt")

    with pytest.raises(check.PackageError):
        check.check_same_resources(staging, tmp_path / "win-unpacked", check.Report())


# --------------------------------------------------------------------------
# The vectors
# --------------------------------------------------------------------------


def test_the_vectors_are_compared_bit_for_bit_on_the_reference_machine(
    check: ModuleType,
) -> None:
    import numpy as np

    expected = np.asarray([[0.5, 0.25], [1.0, 0.0]], dtype=np.float32)
    nudged = expected.copy()
    nudged[1, 0] = np.nextafter(np.float32(1.0), np.float32(0.0))

    assert check.compare_rows(expected.copy(), expected, exact=True) == (
        "2 vectors, bit for bit"
    )
    with pytest.raises(check.PackageError, match="1 of 2 differ in their bits"):
        check.compare_rows(nudged, expected, exact=True)
    assert "within 1e-05" in check.compare_rows(nudged, expected, exact=False)
    with pytest.raises(check.PackageError, match="above"):
        check.compare_rows(expected + 0.01, expected, exact=False)


def test_a_binary_nothing_loads_is_excused_by_name_and_reported(
    check: ModuleType, pe: ModuleType, tmp_path: Path
) -> None:
    """`pgxml.dll` imports `libxslt.dll`, which the runtime has on no machine
    (RFC-041 section 14). Excused by file and reason, and only it: the same
    name wanted by anything else still fails."""
    postgres = tmp_path / "runtime" / "postgres"
    make_pe(postgres / "Library" / "lib" / "pgxml.dll", ["libxslt.dll"])
    windows = fake_windows(pe, tmp_path, protected=set())
    never = (("Library/lib/pgxml.dll", "the xml2 extension"),)
    report = check.Report()

    check.check_dlls(
        [check.Scope("postgres", postgres, never_loaded=never)], report, windows
    )

    assert "0 missing" in report.facts[0]
    assert (
        "libxslt.dll, wanted only by Library/lib/pgxml.dll, is excused"
        in report.facts[1]
    )

    make_pe(postgres / "Library" / "bin" / "postgres.exe", ["libxslt.dll"])
    with pytest.raises(check.PackageError, match="libxslt.dll"):
        check.check_dlls(
            [check.Scope("postgres", postgres, never_loaded=never)],
            check.Report(),
            windows,
        )


def test_the_postgres_runtime_excuses_pgxml_and_nothing_else(check: ModuleType) -> None:
    scopes = {scope.name: scope for scope in check.runtime_scopes(Path("resources"))}

    assert [path for path, _ in scopes["postgres"].never_loaded] == [
        "Library/lib/pgxml.dll"
    ]
    assert scopes["python"].never_loaded == ()
