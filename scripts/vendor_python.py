"""Materialize the embedded Python from the lockfile (RFC-041 section 5).

The installed app runs on the official *embeddable* Python, unpacked in
`runtime\\python` beside the other two runtimes, with its packages installed
in a folder and nothing frozen. This script makes that directory, the way
`vendor_postgres.py` and `vendor_models.py` make theirs:

    python scripts/vendor_python.py           # runtime/python, from the lockfile
    python scripts/vendor_python.py --lock    # rewrite the lockfile from the venv first

Run it with the development virtualenv's interpreter (`backend\\.venv`): its
`pip` installs the wheels, and `--lock` reads its versions.

What it does, in order:

1. Downloads `python-3.12.9-embed-amd64.zip` and checks the sha256 pinned here
   (section 5.1). The origin of the bytes does not matter; the hash decides.
2. Unpacks it into a temporary sibling of the target, and rewrites
   `python312._pth`: the standard library, `Lib\\site-packages` and the
   backend, and nothing else. With that file present Python ignores
   `PYTHONPATH` and the registry, so the path of the code is said there or
   nowhere (section 2.8). **Without `import site`**, unlike section 5.1: in
   3.12.9 the `._pth` turns the isolated mode on and leaves the user's
   site-packages on, and `site` would put `%APPDATA%\\Python\\Python312\\
   site-packages` on the path and run its `.pth` files (RFC-041 section 14).
   The one `.pth` of the lockfile is setuptools' `distutils` shim, which
   nothing the app imports needs.
3. Installs `scripts/python-win-64.lock` into `Lib\\site-packages` with the
   venv's `pip`, `--no-deps --require-hashes`: nothing is resolved here, and a
   wheel whose sha256 is not the lockfile's is refused by `pip` itself.
4. Copies beside `python.exe` the Visual C++ runtime DLLs that the import
   tables ask for and nothing in the tree provides, from the
   `vc14_runtime` the PostgreSQL lockfile already pins (section 5.3), and
   refuses a tree that still lacks one (`pe_imports.py`).
5. Compiles `Lib\\site-packages` with the tree's own `python.exe`, in
   `unchecked-hash` mode: a `.pyc` that does not depend on file dates
   (section 5.4).
6. Checks that what is installed is the lockfile, by `importlib.metadata`
   of the tree's own interpreter; writes `SOLIDVISION-PYTHON.json`; and only
   then renames the directory into place. A run killed half way leaves a
   `.vendor-tmp` directory, never a runtime that looks complete.

`--lock` pins the first part of `backend/requirements.txt` -- the runtime,
before the tests and the tools -- plus `torchvision`, which the app does not
import and whose image processor made every vector of the project (section
2.2), each at the version the venv has. It lets `pip` resolve the closure with
every package constrained to the venv's version, downloads each wheel, checks
that its files are byte for byte the ones installed in the venv (their
`RECORD`), and writes the sha256 of each. A closure that would need a version
the venv does not have fails: the reference vectors came from the venv.

`runtime/` is outside git (`.gitignore`).
"""

from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pe_imports import Windows, resolve_scope, this_windows  # noqa: E402

LOCKFILE = REPO / "scripts" / "python-win-64.lock"
REQUIREMENTS = REPO / "backend" / "requirements.txt"
DEFAULT_TARGET = REPO / "runtime" / "python"
VC_RUNTIME_SOURCE = REPO / "runtime" / "postgres" / "Library" / "bin"
VC_RUNTIME_RECORD = "vc14_runtime-*.json"
"""The conda record of the package the DLLs come from, in
`runtime\\postgres\\conda-meta`: its name, version, build and license go into
the manifest."""
MANIFEST_NAME = "SOLIDVISION-PYTHON.json"
MANIFEST_FORMAT = 1

PYTHON_VERSION = "3.12.9"
"""The venv's, which the suite and the measurements run (section 2.8)."""
EMBED_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_VERSION}/"
    f"python-{PYTHON_VERSION}-embed-amd64.zip"
)
EMBED_SHA256 = "615861fb801e8b04c847598db4e1e46e4b046295017caa37cb5486dde72b5865"
EMBED_SIZE = 11_098_289
"""Computed on 2026-10-26 from the download, whose md5 and size are the ones
python.org publishes for this file (`f34996cc...`, its downloads API)."""

PTH_NAME = "python312._pth"
PTH_LINES = (
    "python312.zip",
    ".",
    r"Lib\site-packages",
    r"..\..\backend",
)
"""Section 5.1, without `import site` (section 14). The paths are relative to
the file: `..\\..\\backend` is the backend of the tree this runtime is in,
whether that is the repository or an installation. `sys.path` is these four,
and nothing a user installed or set reaches it."""
SITE_PACKAGES = Path("Lib", "site-packages")

INDEX_URL = "https://pypi.org/simple"
EXTRA_INDEX_URL = "https://download.pytorch.org/whl/cpu"
"""Where `torch` `+cpu` comes from: the venv's."""
RUNTIME_SECTION_END = "# Testing dependencies"
"""Where the runtime part of `backend/requirements.txt` ends."""
ADDED = ("torchvision",)
SOURCE_ONLY = ("langdetect",)
"""No wheel exists of `langdetect` 1.0.9: its sdist is pinned, and `pip`
builds it with the venv's `setuptools`, without reaching for another."""
TOOLS = ("pytest", "pytest-cov", "black", "ruff", "mypy", "pip")
"""Never in the lockfile: a constraint the closure is not allowed to reach."""

CTYPES_DLLS = ("vcruntime140.dll", "msvcp140.dll", "vcruntime140_1.dll")
"""What `torch/__init__.py` loads with `ctypes.CDLL` by name (2.13.0, lines
245 to 254), which no import table shows (section 2.3)."""

COMPILE_SECONDS = 1800
PIP_SECONDS = 3600


class VendorError(Exception):
    """A step failed. The message says which; nothing half-built is left in place."""


@dataclass(frozen=True)
class Locked:
    """One line of the lockfile."""

    name: str
    version: str
    sha256: str

    def line(self) -> str:
        return f"{self.name}==={self.version} --hash=sha256:{self.sha256}"


# --------------------------------------------------------------------------
# Small things
# --------------------------------------------------------------------------


def mb(size: int) -> float:
    return size / (1024 * 1024)


def dir_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def lockfile_digest(content: bytes) -> str:
    """The lockfile's sha256 with line endings normalized (RFC-033 section 19)."""
    return hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest()


def canonical(name: str) -> str:
    """PEP 503's normalized name, without importing `packaging`."""
    import re

    return re.sub(r"[-_.]+", "-", name).lower()


def pth_text() -> str:
    return "\n".join(PTH_LINES) + "\n"


# --------------------------------------------------------------------------
# The lockfile
# --------------------------------------------------------------------------


def runtime_requirements(requirements: str) -> list[str]:
    """The names of the runtime part of `backend/requirements.txt`, plus
    `torchvision`, as they are written there (`psycopg[binary]`)."""
    names: list[str] = []
    for line in requirements.splitlines():
        if line.strip().startswith(RUNTIME_SECTION_END):
            break
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(line)
    if not names:
        raise VendorError("backend/requirements.txt lists no runtime dependency")
    for added in ADDED:
        if added not in names:
            names.append(added)
    return names


def read_lock(lockfile: Path) -> list[Locked]:
    """The wheels of the lockfile, checked for the shape this script relies on."""
    try:
        text = lockfile.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise VendorError(
            f"{lockfile} is missing: run with --lock to create it"
        ) from None
    return read_lock_text(text, lockfile.name)


def read_lock_text(text: str, name: str = LOCKFILE.name) -> list[Locked]:
    entries: list[Locked] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(("#", "-")):
            continue
        requirement, _, rest = line.partition(" --hash=sha256:")
        project, separator, version = requirement.partition("===")
        sha256 = rest.strip()
        if not separator or not project or not version:
            raise VendorError(f"{name}:{number}: not `name===version`")
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise VendorError(f"{name}:{number}: no sha256 on {project}")
        entries.append(Locked(canonical(project), version, sha256))
    if not entries:
        raise VendorError(f"{name} lists no package")
    names = [entry.name for entry in entries]
    repeated = sorted({project for project in names if names.count(project) > 1})
    if repeated:
        raise VendorError(f"{name} lists {', '.join(repeated)} twice")
    return entries


def lock_text(entries: Iterable[Locked], python: str) -> str:
    header = [
        "# The embedded Python's packages, one wheel per line (RFC-041 section 5.2).",
        "# Generated by `python scripts/vendor_python.py --lock` on "
        f"{datetime.date.today().isoformat()}",
        "# from the runtime part of backend/requirements.txt and torchvision, at "
        "the versions",
        f"# of the development virtualenv (Python {python}), for cp312 win_amd64.",
        "# Installed without a resolver by `python scripts/vendor_python.py`.",
        "# `===` is that exact version: `torchvision===0.28.0` is never 0.28.0+cpu.",
        "# langdetect 1.0.9 has no wheel: its sdist, built with the venv's "
        "setuptools.",
        f"--index-url {INDEX_URL}",
        f"--extra-index-url {EXTRA_INDEX_URL}",
        "--only-binary :all:",
        *(f"--no-binary {name}" for name in SOURCE_ONLY),
    ]
    lines = [entry.line() for entry in sorted(entries, key=lambda e: e.name)]
    return "\n".join([*header, *lines]) + "\n"


def venv_versions() -> dict[str, str]:
    """Canonical name -> normalized version, of the running interpreter."""
    import importlib.metadata

    from packaging.version import Version

    versions: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata["Name"]
        if name:
            versions[canonical(name)] = str(Version(distribution.version))
    return versions


def check_against_venv(resolved: Mapping[str, str], venv: Mapping[str, str]) -> None:
    """Refuse a closure that is not the venv's, package by package.

    The vectors of the reference file came from the venv's packages (section
    2.2): a wheel of another version would make the package something the
    suite never ran.
    """
    problems = []
    for name, version in sorted(resolved.items()):
        if name in TOOLS:
            problems.append(f"{name} is a tool, and the closure reached it")
        elif name not in venv:
            problems.append(f"{name} {version} is not installed in the venv")
        elif venv[name] != version:
            problems.append(f"{name} resolved to {version}, the venv has {venv[name]}")
    if problems:
        raise VendorError("the closure is not the venv's:\n  " + "\n  ".join(problems))


def pinned_requirements(names: Sequence[str], venv: Mapping[str, str]) -> list[str]:
    """Each runtime name at the venv's version, extras kept."""
    pinned = []
    for written in names:
        bare = written.split("[", 1)[0].strip()
        extras = written[len(bare) :].strip()
        version = venv.get(canonical(bare))
        if version is None:
            raise VendorError(
                f"{bare} is in backend/requirements.txt and not in the venv"
            )
        pinned.append(f"{bare}{extras}==={version}")
    return pinned


def parse_distribution_file(name: str) -> tuple[str, str]:
    """(canonical name, version) of a wheel or an sdist, from its file name."""
    from packaging.utils import parse_sdist_filename, parse_wheel_filename

    if name.endswith(".whl"):
        project, version, _, _ = parse_wheel_filename(name)
    else:
        project, version = parse_sdist_filename(name)
    return canonical(str(project)), str(version)


def wheel_record(wheel: Path) -> dict[str, str]:
    """`path -> sha256=...` of a wheel's own `RECORD`, without the files a
    wheel installs elsewhere (`.data`) and without the `RECORD` itself."""
    with zipfile.ZipFile(wheel) as archive:
        names = [
            name
            for name in archive.namelist()
            if name.count("/") == 1 and name.endswith(".dist-info/RECORD")
        ]
        if len(names) != 1:
            raise VendorError(f"{wheel.name} has {len(names)} RECORD files")
        with archive.open(names[0]) as handle:
            rows = list(csv.reader(io.TextIOWrapper(handle, encoding="utf-8")))
    return {
        row[0]: row[1]
        for row in rows
        if len(row) >= 2 and row[1] and ".data/" not in row[0]
    }


def installed_record(project: str) -> dict[str, str]:
    """The same, of the venv's installed distribution."""
    import importlib.metadata

    distribution = importlib.metadata.distribution(project)
    return {
        str(path).replace("\\", "/"): f"{path.hash.mode}={path.hash.value}"
        for path in distribution.files or ()
        if path.hash is not None
    }


def same_files_as_venv(wheel: Path, project: str) -> int:
    """Check that every file of a wheel is the venv's, byte for byte.

    A version number names a release; it does not prove the bytes. Two
    wheels of one version -- a `+cpu` and a plain one, a rebuild -- have the
    same name and not the same files.
    """
    expected = wheel_record(wheel)
    installed = installed_record(project)
    differ = sorted(
        path for path, hash_ in expected.items() if installed.get(path) != hash_
    )
    if differ:
        raise VendorError(
            f"{wheel.name}: {len(differ)} of its files are not the venv's "
            f"({', '.join(differ[:3])})"
        )
    return len(expected)


def pip(arguments: Sequence[str | Path], work: Path) -> None:
    """The venv's `pip`, isolated from the user's configuration and caches."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            *(str(argument) for argument in arguments),
            "--isolated",
            "--disable-pip-version-check",
            "--no-input",
            "--progress-bar",
            "off",
            "--cache-dir",
            str(work / "pip-cache"),
        ],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=PIP_SECONDS,
    )
    if completed.returncode != 0:
        tail = "\n".join((completed.stdout + completed.stderr).splitlines()[-30:])
        raise VendorError(f"pip {arguments[0]} failed:\n{tail}")


def write_lock(work: Path) -> list[Locked]:
    """Resolve the closure at the venv's versions, check it, and write it."""
    _require_the_venv()
    venv = venv_versions()
    names = runtime_requirements(REQUIREMENTS.read_text(encoding="utf-8"))
    top = work / "runtime.txt"
    top.write_text("\n".join(pinned_requirements(names, venv)) + "\n", encoding="utf-8")
    constraints = work / "constraints.txt"
    constraints.write_text(
        "\n".join(
            f"{name}==={version}"
            for name, version in sorted(venv.items())
            if name not in TOOLS
        )
        + "\n",
        encoding="utf-8",
    )
    downloads = work / "downloads"
    pip(
        [
            "download",
            "--requirement",
            top,
            "--constraint",
            constraints,
            "--dest",
            downloads,
            "--index-url",
            INDEX_URL,
            "--extra-index-url",
            EXTRA_INDEX_URL,
            "--only-binary",
            ":all:",
            *(f"--no-binary={name}" for name in SOURCE_ONLY),
            "--no-build-isolation",
        ],
        work,
    )
    files = sorted(downloads.iterdir())
    resolved = dict(parse_distribution_file(path.name) for path in files)
    check_against_venv(resolved, venv)
    entries = []
    for path in files:
        project, version = parse_distribution_file(path.name)
        if path.suffix == ".whl":
            count = same_files_as_venv(path, project)
            how = f"{count} files equal to the venv's"
        else:
            how = "an sdist"
        entries.append(Locked(project, version, file_sha256(path)))
        print(f"  {project} {version}: {mb(path.stat().st_size):.1f} MB, {how}")
    LOCKFILE.write_text(
        lock_text(entries, sys.version.split()[0]), encoding="utf-8", newline="\n"
    )
    total = sum(path.stat().st_size for path in files)
    print(f"{LOCKFILE.relative_to(REPO)}: {len(entries)} packages, {mb(total):.0f} MB")
    return entries


def _require_the_venv() -> None:
    if sys.prefix == sys.base_prefix:
        raise VendorError(
            f"{sys.executable} is not a virtualenv: run this with the development "
            "one (backend\\.venv\\Scripts\\python.exe)."
        )


# --------------------------------------------------------------------------
# Materializing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Steps:
    """The parts of a materialization that reach outside the tree being made.

    Each is replaced in the tests, which build a tree from a zip of their own
    and an install that writes a few files.
    """

    embed_zip: Callable[[Path], Path]
    """work -> the checked embeddable zip."""
    install: Callable[[Path, Path, Path], None]
    """(site-packages, lockfile, work) -> the wheels installed there."""
    windows: Callable[[], Windows]
    vc_source: Path
    compile: Callable[[Path], None]
    """The tree -> its `site-packages` compiled by its own interpreter."""
    installed: Callable[[Path], dict[str, str]]
    """The tree -> canonical name -> version, by its own interpreter."""


def fetch_embed_zip(work: Path) -> Path:
    archive = work / EMBED_URL.rsplit("/", 1)[1]
    request = urllib.request.Request(
        EMBED_URL, headers={"User-Agent": "SolidVision vendor_python"}
    )
    with (
        urllib.request.urlopen(request, timeout=300) as response,
        archive.open("wb") as handle,
    ):
        shutil.copyfileobj(response, handle)
    check_embed_zip(archive)
    return archive


def check_embed_zip(archive: Path) -> None:
    found = file_sha256(archive)
    if found != EMBED_SHA256:
        raise VendorError(
            f"{archive.name}: sha256 {found}, pinned {EMBED_SHA256}. "
            "Nothing was changed."
        )
    print(f"python {PYTHON_VERSION} embeddable: sha256 OK")


def install_lock(site_packages: Path, lockfile: Path, work: Path) -> None:
    pip(
        [
            "install",
            "--requirement",
            lockfile,
            "--target",
            site_packages,
            "--no-deps",
            "--require-hashes",
            "--no-build-isolation",
            "--no-compile",
            "--no-warn-script-location",
        ],
        work,
    )


def compile_site_packages(tree: Path) -> None:
    """Every `.py` of `site-packages`, by the tree's own `python.exe`.

    `unchecked-hash`: the `.pyc` is not compared with the source's date, so it
    does not matter whether an extraction kept the dates (section 5.4). A
    file that does not compile -- a template, a test written for another
    Python -- is left without one: nothing imports it, and the count goes to
    the log.
    """
    completed = subprocess.run(
        [
            str(tree / "python.exe"),
            "-X",
            "utf8",
            "-m",
            "compileall",
            "-q",
            "-j",
            "0",
            "--invalidation-mode",
            "unchecked-hash",
            str(tree / SITE_PACKAGES),
        ],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=COMPILE_SECONDS,
    )
    failed = [line for line in completed.stdout.splitlines() if line.startswith("***")]
    if completed.returncode != 0 and not failed:
        raise VendorError(f"compileall failed: {completed.stderr.strip()[-500:]}")
    if failed:
        print(f"  {len(failed)} files did not compile, and are left as source:")
        for line in failed[:10]:
            print(f"    {line}")


DISTRIBUTIONS_PROBE = """
import importlib.metadata, json, sys
found = {}
for distribution in importlib.metadata.distributions(path=[sys.argv[1]]):
    found[distribution.metadata["Name"]] = distribution.version
print(json.dumps(found))
"""


def installed_by_its_python(tree: Path) -> dict[str, str]:
    """What `site-packages` holds, as the tree's own interpreter reads it."""
    completed = subprocess.run(
        [
            str(tree / "python.exe"),
            "-X",
            "utf8",
            "-c",
            DISTRIBUTIONS_PROBE,
            str(tree / SITE_PACKAGES),
        ],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if completed.returncode != 0:
        raise VendorError(
            f"{tree / 'python.exe'} could not list its packages: "
            f"{completed.stderr.strip()[-500:]}"
        )
    found = json.loads(completed.stdout)
    return {canonical(name): version for name, version in found.items()}


def compare_with_lock(lock: Sequence[Locked], installed: Mapping[str, str]) -> None:
    """What is installed is the lockfile, neither more nor less."""
    expected = {entry.name: entry.version for entry in lock}
    problems = [
        f"{name} {version} is in the lockfile and not installed"
        for name, version in sorted(expected.items())
        if name not in installed
    ]
    problems += [
        f"{name} {version} is installed and not in the lockfile"
        for name, version in sorted(installed.items())
        if name not in expected
    ]
    problems += [
        f"{name} is {installed[name]}, the lockfile says {version}"
        for name, version in sorted(expected.items())
        if name in installed and _normalized(installed[name]) != _normalized(version)
    ]
    if problems:
        raise VendorError(
            "site-packages is not the lockfile:\n  " + "\n  ".join(problems)
        )


def _normalized(version: str) -> str:
    try:
        from packaging.version import Version
    except ImportError:  # the embedded Python checking itself: no normalizing
        return version.lower()
    return str(Version(version))


def default_steps() -> Steps:
    return Steps(
        embed_zip=fetch_embed_zip,
        install=install_lock,
        windows=this_windows,
        vc_source=VC_RUNTIME_SOURCE,
        compile=compile_site_packages,
        installed=installed_by_its_python,
    )


def unpack(archive: Path, tree: Path) -> None:
    with zipfile.ZipFile(archive) as opened:
        for member in opened.namelist():
            if member.startswith(("/", "\\")) or ".." in Path(member).parts:
                raise VendorError(f"{archive.name} has an unsafe member: {member}")
        opened.extractall(tree)
    if not (tree / "python.exe").is_file() or not (tree / PTH_NAME).is_file():
        raise VendorError(f"{archive.name} has no python.exe or {PTH_NAME}")
    (tree / PTH_NAME).write_text(pth_text(), encoding="utf-8")


def remove_scripts(site_packages: Path) -> list[str]:
    """`pip --target` writes console scripts to `bin`, as launchers of the
    venv's interpreter, with this machine's path in them. The app runs none."""
    scripts = site_packages / "bin"
    if not scripts.is_dir():
        return []
    removed = sorted(path.name for path in scripts.iterdir())
    shutil.rmtree(scripts)
    return removed


def add_vc_runtime(tree: Path, windows: Windows, source: Path) -> dict[str, str]:
    """Copy beside `python.exe` the DLLs the tree asks for and lacks.

    Only what the import tables and `torch`'s `ctypes` ask for, and only from
    `source`: a name the source does not have either is left missing, and the
    check after this names it.
    """
    report = resolve_scope(tree, windows, by_name=CTYPES_DLLS)
    copied: dict[str, str] = {}
    for missing in report.missing:
        candidate = source / missing.name
        matches = [
            path for path in source.glob("*.dll") if path.name.lower() == missing.name
        ]
        if matches:
            candidate = matches[0]
        if candidate.is_file():
            shutil.copy2(candidate, tree / candidate.name)
            copied[candidate.name] = file_sha256(candidate)
    if copied:
        # A DLL brought in may import another one.
        copied.update(add_vc_runtime(tree, windows, source))
    return copied


def check_dlls(tree: Path, windows: Windows) -> int:
    report = resolve_scope(tree, windows, by_name=CTYPES_DLLS)
    if report.missing:
        raise VendorError(
            "the tree asks for DLLs nothing provides:\n  "
            + "\n  ".join(missing.line() for missing in report.missing)
        )
    return report.files


def vc_runtime_package(source: Path) -> dict[str, Any]:
    """Name, version, build and license of the package the DLLs come from."""
    records = sorted((source.parents[1] / "conda-meta").glob(VC_RUNTIME_RECORD))
    if len(records) != 1:
        return {}
    record = json.loads(records[0].read_text(encoding="utf-8"))
    return {
        key: record.get(key) for key in ("name", "version", "build", "license", "url")
    }


def materialize(
    lockfile: Path, target: Path, steps: Steps, work: Path
) -> dict[str, Any]:
    """Build the tree beside `target` and rename it into place, or leave none."""
    lock = read_lock(lockfile)
    staging = target.with_name(target.name + ".vendor-tmp")
    shutil.rmtree(staging, ignore_errors=True)
    if staging.exists():
        raise VendorError(f"{staging} is left over and could not be removed")
    staging.parent.mkdir(parents=True, exist_ok=True)
    try:
        unpack(steps.embed_zip(work), staging)
        site_packages = staging / SITE_PACKAGES
        site_packages.mkdir(parents=True)
        steps.install(site_packages, lockfile, work)
        scripts = remove_scripts(site_packages)
        if scripts:
            print(f"  removed {len(scripts)} console scripts pip wrote for the venv")
        windows = steps.windows()
        dlls = add_vc_runtime(staging, windows, steps.vc_source)
        print(
            "  Visual C++ runtime beside python.exe: "
            f"{', '.join(sorted(dlls)) or 'none'}"
        )
        pe_files = check_dlls(staging, windows)
        print(f"  {pe_files} PE files, every import found in the tree or in Windows")
        steps.compile(staging)
        installed = steps.installed(staging)
        compare_with_lock(lock, installed)
        print(f"  {len(installed)} packages installed, the lockfile's")
        manifest = {
            "format": MANIFEST_FORMAT,
            "python": PYTHON_VERSION,
            "embeddable_sha256": EMBED_SHA256,
            "lockfile": lockfile.name,
            "lockfile_sha256": lockfile_digest(lockfile.read_bytes()),
            "packages": len(lock),
            "vc_runtime": {
                "dlls": dict(sorted(dlls.items())),
                "package": vc_runtime_package(steps.vc_source),
            },
            "pth": list(PTH_LINES),
            "bytecode": "unchecked-hash",
            "materialized_at": datetime.datetime.now()
            .astimezone()
            .isoformat(timespec="seconds"),
        }
        manifest["bytes"] = dir_size(staging)
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    swap_into_place(staging, target)
    return manifest


def running_from(tree: Path) -> bool:
    """Whether the tree's `python.exe` is executing: Windows refuses to open a
    running image for writing (`vendor_postgres.py`)."""
    executable = tree / "python.exe"
    if not executable.is_file():
        return False
    try:
        with executable.open("r+b"):
            return False
    except PermissionError:
        return True


def swap_into_place(staging: Path, target: Path) -> None:
    if running_from(target):
        raise VendorError(
            f"{target} is in use: a python.exe is running from it. Stop it and run "
            f"this again. Nothing was changed; the new tree is waiting in {staging}."
        )
    retired = target.with_name(target.name + ".vendor-old")
    shutil.rmtree(retired, ignore_errors=True)
    if target.exists():
        target.rename(retired)
    try:
        staging.rename(target)
    except OSError:
        if retired.exists():
            retired.rename(target)
        raise
    shutil.rmtree(retired, ignore_errors=True)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--target",
        type=Path,
        default=DEFAULT_TARGET,
        help=f"where the tree goes (default: {DEFAULT_TARGET})",
    )
    parser.add_argument(
        "--lock",
        action="store_true",
        help="rewrite the lockfile from the venv's versions first",
    )
    args = parser.parse_args()
    if (
        sys.platform != "win32"
        or sys.maxsize <= 2**32
        or sys.version_info[:2] != (3, 12)
    ):
        print("The runtime is cp312 win_amd64: run this with the venv's Python 3.12.")
        return 2
    target: Path = args.target.resolve()
    work = Path(tempfile.mkdtemp(prefix="solidvision-python-"))
    try:
        if args.lock:
            write_lock(work)
        manifest = materialize(LOCKFILE, target, default_steps(), work)
        print(f"python: {target}")
        print(
            f"  {PYTHON_VERSION}, {manifest['packages']} packages, "
            f"{mb(manifest['bytes']):.0f} MB, "
            f"lockfile {manifest['lockfile_sha256'][:12]}"
        )
        return 0
    except (VendorError, OSError, subprocess.TimeoutExpired) as error:
        print(f"FAILED: {error}")
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
