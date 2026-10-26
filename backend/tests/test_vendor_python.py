"""The embedded Python is the lockfile's, or there is none (RFC-041 section 5).

Two halves. The real lockfile, `scripts/python-win-64.lock`, is read here and
held against the app and the venv: every third-party import of `backend/app`
has its package in it, `torchvision` is in it, each version is the venv's --
the reference vectors came from the venv (section 2.2) -- and no tool is.

And `scripts/vendor_python.py` builds its tree in a miniature: an embeddable
zip of a few files, an "install" that writes two, a `System32` of
make-believe. Nothing here reaches the network or runs `pip`; the real tree is
made by the script and checked by `check_package.py` (section 9).
"""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import importlib.util
import json
import sys
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.pe_files import make_pe

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
APP = REPO / "backend" / "app"


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
def vendor() -> ModuleType:
    return load("vendor_python")


@pytest.fixture(scope="module")
def locked(vendor: ModuleType) -> dict[str, str]:
    """Canonical name -> version, of the real lockfile."""
    return {entry.name: entry.version for entry in vendor.read_lock(vendor.LOCKFILE)}


# --------------------------------------------------------------------------
# The lockfile (section 5.2)
# --------------------------------------------------------------------------


def third_party_imports() -> set[str]:
    """The top-level modules `backend/app` imports, by its syntax tree."""
    names: set[str] = set()
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return {name for name in names if name not in sys.stdlib_module_names} - {"app"}


def test_every_third_party_import_of_the_app_is_in_the_lockfile(
    vendor: ModuleType, locked: dict[str, str]
) -> None:
    providers = importlib.metadata.packages_distributions()
    uncovered = []
    for module in sorted(third_party_imports()):
        distributions = {vendor.canonical(name) for name in providers.get(module, [])}
        if not distributions & set(locked):
            uncovered.append(f"{module} ({', '.join(sorted(distributions)) or '?'})")

    assert uncovered == [], "imported by the app and not in the lockfile"
    assert {"torch", "transformers", "PIL", "fastapi"} <= third_party_imports()


def test_torchvision_is_in_it_though_the_app_never_imports_it(
    locked: dict[str, str],
) -> None:
    """Section 2.2: its processor made every vector of the project."""
    assert "torchvision" not in third_party_imports()
    assert locked["torchvision"] == importlib.metadata.version("torchvision")


def test_torch_is_the_cpu_build(locked: dict[str, str]) -> None:
    """`torch` comes from the CPU index. `torchvision` is the venv's own, from
    PyPI: on Windows that wheel is a CPU build without the `+cpu` label, and a
    `0.28.0+cpu` would be another file (section 14)."""
    assert locked["torch"].endswith("+cpu")
    assert not locked["torchvision"].endswith("+cpu")


def test_every_version_is_the_venvs(vendor: ModuleType, locked: dict[str, str]) -> None:
    """Section 13: updating one without the other is section 2.2 again."""
    venv = vendor.venv_versions()

    differ = {
        name: (version, venv.get(name))
        for name, version in locked.items()
        if venv.get(name) != version
    }
    assert differ == {}


def test_no_tool_is_in_it(locked: dict[str, str]) -> None:
    assert not {"pytest", "pytest-cov", "black", "ruff", "mypy", "pip"} & set(locked)


def test_every_line_has_a_sha256_and_the_header_the_two_indexes(
    vendor: ModuleType,
) -> None:
    lines = vendor.LOCKFILE.read_text(encoding="utf-8").splitlines()
    packages = [line for line in lines if line and not line.startswith(("#", "-"))]
    options = [line for line in lines if line.startswith("-")]

    assert packages and all(" --hash=sha256:" in line for line in packages)
    assert all(len(line.rsplit(":", 1)[1]) == 64 for line in packages)
    assert all("===" in line for line in packages)
    assert options[:2] == [
        "--index-url https://pypi.org/simple",
        "--extra-index-url https://download.pytorch.org/whl/cpu",
    ]
    assert "--only-binary :all:" in options


def test_the_lockfile_is_read_as_the_script_writes_it(vendor: ModuleType) -> None:
    entries = [
        vendor.Locked("six", "1.17.0", "a" * 64),
        vendor.Locked("abc", "1", "b" * 64),
    ]
    text = vendor.lock_text(entries, "3.12.9")

    assert [e.name for e in vendor.read_lock_text(text)] == ["abc", "six"]


@pytest.mark.parametrize(
    ("line", "match"),
    [
        ("six==1.17.0 --hash=sha256:" + "a" * 64, "name===version"),
        ("six===1.17.0", "no sha256"),
        ("six===1.17.0 --hash=sha256:abc", "no sha256"),
    ],
)
def test_a_line_that_is_not_pinned_and_hashed_is_refused(
    vendor: ModuleType, tmp_path: Path, line: str, match: str
) -> None:
    lockfile = tmp_path / "python-win-64.lock"
    lockfile.write_text(
        f"--index-url https://pypi.org/simple\n{line}\n", encoding="utf-8"
    )

    with pytest.raises(vendor.VendorError, match=match):
        vendor.read_lock(lockfile)


def test_a_package_listed_twice_is_refused(vendor: ModuleType, tmp_path: Path) -> None:
    lockfile = tmp_path / "python-win-64.lock"
    line = "six===1.17.0 --hash=sha256:" + "a" * 64
    lockfile.write_text(f"{line}\n{line.replace('six', 'Six')}\n", encoding="utf-8")

    with pytest.raises(vendor.VendorError, match="twice"):
        vendor.read_lock(lockfile)


# --------------------------------------------------------------------------
# --lock: the closure is the venv's
# --------------------------------------------------------------------------


def test_the_runtime_part_of_the_requirements_and_torchvision(
    vendor: ModuleType,
) -> None:
    requirements = (
        "# Runtime\nfastapi\npsycopg[binary]  # the binary\n\n"
        "# Testing dependencies for unit tests\npytest\n# Development\nmypy\n"
    )

    assert vendor.runtime_requirements(requirements) == [
        "fastapi",
        "psycopg[binary]",
        "torchvision",
    ]


def test_each_runtime_name_is_pinned_at_the_venvs_version_with_its_extras(
    vendor: ModuleType,
) -> None:
    venv = {"psycopg": "3.3.4", "torch": "2.13.0+cpu"}

    assert vendor.pinned_requirements(["psycopg[binary]", "torch"], venv) == [
        "psycopg[binary]===3.3.4",
        "torch===2.13.0+cpu",
    ]
    with pytest.raises(vendor.VendorError, match="not in the venv"):
        vendor.pinned_requirements(["onnx"], venv)


def test_a_closure_version_other_than_the_venvs_fails_the_lock(
    vendor: ModuleType,
) -> None:
    venv = {"numpy": "2.5.2", "torchvision": "0.28.0", "pytest": "9.1.1"}

    vendor.check_against_venv({"numpy": "2.5.2", "torchvision": "0.28.0"}, venv)
    with pytest.raises(
        vendor.VendorError, match=r"torchvision resolved to 0.28.0\+cpu"
    ):
        vendor.check_against_venv({"torchvision": "0.28.0+cpu"}, venv)
    with pytest.raises(vendor.VendorError, match="scipy 1.0 is not installed"):
        vendor.check_against_venv({"scipy": "1.0"}, venv)
    with pytest.raises(vendor.VendorError, match="pytest is a tool"):
        vendor.check_against_venv({"pytest": "9.1.1"}, venv)


def write_wheel(path: Path, record: dict[str, str]) -> Path:
    rows = "".join(f"{name},{hash_},1\n" for name, hash_ in record.items())
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "six-1.17.0.dist-info/RECORD", rows + "six-1.17.0.dist-info/RECORD,,\n"
        )
    return path


def test_a_wheel_is_accepted_only_with_the_files_the_venv_has(
    vendor: ModuleType, tmp_path: Path
) -> None:
    """The version names a release; the files' hashes prove the bytes."""
    installed = vendor.installed_record("six")
    same = {
        path: hash_
        for path, hash_ in installed.items()
        if not path.endswith("INSTALLER")
    }
    assert vendor.same_files_as_venv(
        write_wheel(tmp_path / "a.whl", same), "six"
    ) == len(same)

    other = dict(same)
    other["six.py"] = "sha256=" + "A" * 43
    with pytest.raises(vendor.VendorError, match="not the venv's"):
        vendor.same_files_as_venv(write_wheel(tmp_path / "b.whl", other), "six")


# --------------------------------------------------------------------------
# The ._pth (section 5.1)
# --------------------------------------------------------------------------


def test_the_pth_is_written_line_by_line(vendor: ModuleType) -> None:
    assert vendor.pth_text().splitlines() == [
        "python312.zip",
        ".",
        "Lib\\site-packages",
        "..\\..\\backend",
    ]


def test_the_pth_does_not_import_site(vendor: ModuleType) -> None:
    """Section 14: in 3.12.9 the `._pth` leaves the user's site-packages on,
    and `site` would add it, `.pth` files and all."""
    assert "import site" not in vendor.pth_text()


# --------------------------------------------------------------------------
# Materializing, in a miniature
# --------------------------------------------------------------------------


@pytest.fixture()
def embed_zip(tmp_path: Path) -> Path:
    """An embeddable zip of a few files, laid out like the real one."""
    parts = tmp_path / "embed"
    make_pe(parts / "python.exe", ["python312.dll", "kernel32.dll"])
    make_pe(parts / "python312.dll", ["vcruntime140.dll", "kernel32.dll"])
    make_pe(parts / "vcruntime140.dll", ["kernel32.dll"])
    make_pe(parts / "vcruntime140_1.dll", ["kernel32.dll"])
    (parts / "python312._pth").write_text("python312.zip\n.\n\n# import site\n")
    with zipfile.ZipFile(parts / "python312.zip", "w"):
        pass
    archive = tmp_path / "python-3.12.9-embed-amd64.zip"
    with zipfile.ZipFile(archive, "w") as writing:
        for file in sorted(parts.iterdir()):
            writing.write(file, file.name)
    return archive


@pytest.fixture()
def postgres(tmp_path: Path) -> Path:
    """`runtime\\postgres\\Library\\bin`, with the Visual C++ runtime in it."""
    root = tmp_path / "postgres"
    bin_ = root / "Library" / "bin"
    for name in ("msvcp140.dll", "msvcp140_atomic_wait.dll", "concrt140.dll"):
        make_pe(bin_ / name, ["kernel32.dll"])
    (root / "conda-meta").mkdir()
    (root / "conda-meta" / "vc14_runtime-14.51.36247-habf1de7_41.json").write_text(
        json.dumps(
            {
                "name": "vc14_runtime",
                "version": "14.51.36247",
                "build": "habf1de7_41",
                "license": "LicenseRef-MicrosoftVisualCpp2015-2022Runtime",
                "url": "https://conda.anaconda.org/x.conda",
            }
        )
    )
    return bin_


@pytest.fixture()
def lockfile(tmp_path: Path) -> Path:
    path = tmp_path / "python-win-64.lock"
    path.write_bytes(
        b"--index-url https://pypi.org/simple\r\nsix===1.17.0 --hash=sha256:"
        + b"a" * 64
        + b"\r\n"
    )
    return path


def steps(
    vendor: ModuleType,
    embed_zip: Path,
    postgres: Path,
    *,
    imports: tuple[str, ...] = ("kernel32.dll",),
    installed: dict[str, str] | None = None,
    failure: BaseException | None = None,
) -> Any:
    pe = load("pe_imports")

    def install(site_packages: Path, lock: Path, work: Path) -> None:
        if failure is not None:
            raise failure
        (site_packages / "six.py").write_text("VALUE = 1\n")
        (site_packages / "six-1.17.0.dist-info").mkdir()
        make_pe(site_packages / "_native.pyd", list(imports))
        (site_packages / "bin").mkdir()
        (site_packages / "bin" / "six-console.exe").write_bytes(b"MZ venv launcher")

    def windows() -> Any:
        return pe.Windows(
            postgres.parent / "System32", frozenset({"kernel32.dll"}), lambda _: False
        )

    return vendor.Steps(
        embed_zip=lambda work: embed_zip,
        install=install,
        windows=windows,
        vc_source=postgres,
        compile=lambda tree: (tree / "compiled").write_text("yes"),
        installed=lambda tree: (
            installed if installed is not None else {"six": "1.17.0"}
        ),
    )


def test_a_materialization_makes_the_tree_and_its_manifest(
    vendor: ModuleType, embed_zip: Path, postgres: Path, lockfile: Path, tmp_path: Path
) -> None:
    target = tmp_path / "runtime" / "python"

    manifest = vendor.materialize(
        lockfile, target, steps(vendor, embed_zip, postgres), tmp_path
    )

    assert (target / "python312._pth").read_text().splitlines() == list(
        vendor.PTH_LINES
    )
    assert (target / "Lib" / "site-packages" / "six.py").is_file()
    assert not (
        target / "Lib" / "site-packages" / "bin"
    ).exists(), "the venv's launchers"
    assert (target / "compiled").read_text() == "yes"
    assert not target.with_name("python.vendor-tmp").exists()
    written = json.loads((target / vendor.MANIFEST_NAME).read_text())
    assert written == manifest
    assert manifest["format"] == 1
    assert manifest["python"] == "3.12.9"
    assert manifest["packages"] == 1
    assert manifest["bytecode"] == "unchecked-hash"
    assert manifest["pth"] == list(vendor.PTH_LINES)
    # CRLF or LF, the same lockfile: the digest normalizes the line endings.
    normalized = lockfile.read_bytes().replace(b"\r\n", b"\n")
    assert manifest["lockfile_sha256"] == hashlib.sha256(normalized).hexdigest()


def test_the_visual_cpp_runtime_asked_for_is_copied_beside_python_exe(
    vendor: ModuleType, embed_zip: Path, postgres: Path, lockfile: Path, tmp_path: Path
) -> None:
    """Section 5.3: what the import tables and `torch`'s `ctypes` ask for, from
    the `vc14_runtime` the PostgreSQL lockfile pins, and nothing else."""
    target = tmp_path / "runtime" / "python"
    asked = steps(vendor, embed_zip, postgres, imports=("MSVCP140_ATOMIC_WAIT.dll",))

    manifest = vendor.materialize(lockfile, target, asked, tmp_path)

    dlls = manifest["vc_runtime"]["dlls"]
    assert sorted(dlls) == ["msvcp140.dll", "msvcp140_atomic_wait.dll"]
    assert (target / "msvcp140.dll").read_bytes() == (
        postgres / "msvcp140.dll"
    ).read_bytes()
    assert not (target / "concrt140.dll").exists()
    assert manifest["vc_runtime"]["package"]["name"] == "vc14_runtime"
    assert manifest["vc_runtime"]["package"]["version"] == "14.51.36247"


def test_a_dll_nothing_provides_fails_and_leaves_no_tree(
    vendor: ModuleType, embed_zip: Path, postgres: Path, lockfile: Path, tmp_path: Path
) -> None:
    target = tmp_path / "runtime" / "python"

    with pytest.raises(vendor.VendorError, match="nowhere.dll: nowhere"):
        vendor.materialize(
            lockfile,
            target,
            steps(vendor, embed_zip, postgres, imports=("nowhere.dll",)),
            tmp_path,
        )

    assert not target.exists()
    assert not target.with_name("python.vendor-tmp").exists()


def test_an_interrupted_materialization_leaves_the_previous_tree_as_it_was(
    vendor: ModuleType, embed_zip: Path, postgres: Path, lockfile: Path, tmp_path: Path
) -> None:
    target = tmp_path / "runtime" / "python"
    target.mkdir(parents=True)
    (target / "the-old-tree").write_text("kept")

    with pytest.raises(KeyboardInterrupt):
        vendor.materialize(
            lockfile,
            target,
            steps(vendor, embed_zip, postgres, failure=KeyboardInterrupt()),
            tmp_path,
        )

    assert [path.name for path in target.iterdir()] == ["the-old-tree"]
    assert not target.with_name("python.vendor-tmp").exists()


@pytest.mark.parametrize(
    ("installed", "match"),
    [
        ({"six": "1.16.0"}, "six is 1.16.0, the lockfile says 1.17.0"),
        ({}, "six 1.17.0 is in the lockfile and not installed"),
        ({"six": "1.17.0", "onnx": "1.22.0"}, "onnx 1.22.0 is installed and not in"),
    ],
)
def test_site_packages_must_be_the_lockfile_neither_more_nor_less(
    vendor: ModuleType,
    embed_zip: Path,
    postgres: Path,
    lockfile: Path,
    tmp_path: Path,
    installed: dict[str, str],
    match: str,
) -> None:
    target = tmp_path / "runtime" / "python"

    with pytest.raises(vendor.VendorError, match=match):
        vendor.materialize(
            lockfile,
            target,
            steps(vendor, embed_zip, postgres, installed=installed),
            tmp_path,
        )

    assert not target.exists()


def test_an_embeddable_zip_of_another_hash_is_refused(
    vendor: ModuleType, tmp_path: Path
) -> None:
    archive = tmp_path / "python-3.12.9-embed-amd64.zip"
    archive.write_bytes(b"not python")

    with pytest.raises(vendor.VendorError, match="sha256"):
        vendor.check_embed_zip(archive)


def test_a_zip_member_outside_the_tree_is_refused(
    vendor: ModuleType, tmp_path: Path
) -> None:
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as writing:
        writing.writestr("../outside.txt", "x")

    with pytest.raises(vendor.VendorError, match="unsafe member"):
        vendor.unpack(archive, tmp_path / "tree")

    assert not (tmp_path / "outside.txt").exists()
