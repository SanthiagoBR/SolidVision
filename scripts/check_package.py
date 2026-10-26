"""The checks of the package (RFC-041 section 7.3). Each one fails the build.

    python scripts/check_package.py --staging DIR      # the montagem, resources\\
    python scripts/check_package.py --staging DIR --unpacked DIR
                                                       # electron-builder's folder,
                                                       # against the montagem
    <montagem>\\runtime\\python\\python.exe scripts/check_package.py \\
        --reference backend/tests/fixtures/rfc035_reference_embeddings.npz \\
        --images backend/dataset/demo                 # the vectors, by the package

On the montagem -- the folder `npm run dist` hands to electron-builder as
`resources\\` --:

- **the tree**: no `pyproject.toml`, `.env` nor `tests\\` (the guard of the
  staging), and each part present;
- **the lockfile**: `Lib\\site-packages` is the lockfile, read by the
  package's own `importlib.metadata`, and `runtime\\python` was made from
  today's lockfile;
- **the path**: every file fits in 259 characters under the longest profile
  path (section 2.5);
- **the DLLs**: every import of every binary is found in its runtime or in
  Windows (`pe_imports.py`);
- **the vectors**: the 45 images and the 50 queries of RFC-035 section 15, by
  the package's Python in the installed mode, bit for bit against the
  reference file.

On electron-builder's folder: the path again, now with the Electron files;
the DLLs of the three runtimes, `SolidVision.exe` included; `resources\\` the
same files as the montagem, without its marker; and the `app.asar` listing.

**Run by the package's Python, with `--reference`, this imports nothing but
the standard library, `numpy` and the package's own `app`.** The other checks
import `pe_imports`, `vendor_python` and `stage_installation` when they run:
the last one puts the repository's `backend` first on the path, which would
make the vectors the checkout's.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

LONGEST_USER_NAME = 20
"""The longest name a local Windows account takes."""
INSTALL_PREFIX = (
    "C:\\Users\\"
    + "x" * LONGEST_USER_NAME
    + "\\AppData\\Local\\Programs\\SolidVision\\"
)
"""The installation folder of the longest profile path: 65 characters."""
RESOURCES_PREFIX = INSTALL_PREFIX + "resources\\"
"""What the montagem becomes once installed: 75 characters (section 2.5)."""
MAX_PATH_CHARS = 259
"""`MAX_PATH` is 260 with the terminating NUL."""

REFERENCE = REPO / "backend" / "tests" / "fixtures" / "rfc035_reference_embeddings.npz"
DEMO = REPO / "backend" / "dataset" / "demo"
STAGED_MARKER = "SOLIDVISION-STAGED.json"

ASAR_ALLOWED = ("dist-electron/", "electron/startup.html", "package.json")
"""What the `app.asar` holds, and nothing else (section 7.1): the shell. The
page's dependencies are in the interface's build, which the API serves."""

TOLERANCE = 1e-5
"""The tolerance of `test_reference_embeddings_slow.py`, for a machine that is
not the one that wrote the reference. On that one, the bits."""
VECTORS_SECONDS = 1800


class PackageError(Exception):
    """A check failed. The message says which, and what was found."""


@dataclass
class Report:
    """What each check found, printed at the end and written to the log."""

    facts: list[str] = field(default_factory=list)

    def add(self, line: str) -> None:
        print(f"  {line}", flush=True)
        self.facts.append(line)


# --------------------------------------------------------------------------
# The path budget (section 2.5)
# --------------------------------------------------------------------------


def windows_relative(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace("/", "\\")


def over_budget(root: Path, prefix: str) -> list[tuple[int, str]]:
    """(full length, relative path) of every file whose installed path would
    pass `MAX_PATH_CHARS`, longest first."""
    found = []
    for directory, _, names in os.walk(root):
        for name in names:
            relative = windows_relative(Path(directory) / name, root)
            length = len(prefix) + len(relative)
            if length > MAX_PATH_CHARS:
                found.append((length, relative))
    return sorted(found, reverse=True)


def longest_path(root: Path, prefix: str) -> tuple[int, str]:
    longest = (0, "")
    for directory, _, names in os.walk(root):
        for name in names:
            relative = windows_relative(Path(directory) / name, root)
            longest = max(longest, (len(prefix) + len(relative), relative))
    return longest


def license_tree_of(path: Path, root: Path) -> Path | None:
    """The `licenses` folder of a `dist-info` that holds `path`, if any."""
    for parent in path.parents:
        if parent == root:
            return None
        if parent.name == "licenses" and parent.parent.name.endswith(".dist-info"):
            return parent
    return None


def zip_license_trees(root: Path, prefix: str) -> list[str]:
    """Each `licenses` tree of a `dist-info` that passes the budget becomes a
    `licenses.zip` in the same place, with the paths inside it preserved.

    The notices stay in the package; nothing at run time reads those folders.
    Any other file above the budget is left for `check_path_budget`, which
    fails on it.
    """
    trees = {
        tree
        for _, relative in over_budget(root, prefix)
        if (tree := license_tree_of(root / relative, root)) is not None
    }
    zipped = []
    for tree in sorted(trees):
        archive = tree.with_name("licenses.zip")
        if archive.exists():
            raise PackageError(f"{archive} exists already, beside {tree.name}")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as writing:
            for file in sorted(path for path in tree.rglob("*") if path.is_file()):
                writing.write(file, file.relative_to(tree).as_posix())
        shutil.rmtree(tree)
        zipped.append(windows_relative(archive, root))
    return zipped


def check_path_budget(root: Path, prefix: str, report: Report, what: str) -> None:
    over = over_budget(root, prefix)
    if over:
        lines = [f"{length}: {prefix}{relative}" for length, relative in over[:5]]
        raise PackageError(
            f"{len(over)} files of {what} pass {MAX_PATH_CHARS} characters once "
            "installed:\n  " + "\n  ".join(lines)
        )
    length, relative = longest_path(root, prefix)
    report.add(f"path, {what}: the longest is {length} of {MAX_PATH_CHARS}: {relative}")


# --------------------------------------------------------------------------
# The tree and the lockfile
# --------------------------------------------------------------------------


REQUIRED = (
    "backend/app/__init__.py",
    "backend/alembic.ini",
    "frontend/dist/index.html",
    "runtime/python/python.exe",
    "runtime/python/python312._pth",
    "runtime/python/SOLIDVISION-PYTHON.json",
    "runtime/postgres/SOLIDVISION-RUNTIME.json",
    "runtime/models/SOLIDVISION-MODELS.json",
)


def check_tree(staging: Path, report: Report) -> None:
    from stage_installation import StagingError, _refuse_a_checkout

    try:
        _refuse_a_checkout(staging)
    except StagingError as error:
        raise PackageError(str(error)) from None
    missing = [name for name in REQUIRED if not (staging / name).is_file()]
    if missing:
        raise PackageError(f"{staging} has no {', '.join(missing)}")
    report.add(
        "tree: no pyproject.toml, .env nor tests; the backend, the interface "
        "and the three runtimes are there"
    )


def check_lockfile(staging: Path, report: Report) -> None:
    import vendor_python

    python = staging / "runtime" / "python"
    lock = vendor_python.read_lock(vendor_python.LOCKFILE)
    try:
        installed = vendor_python.installed_by_its_python(python)
        vendor_python.compare_with_lock(lock, installed)
    except vendor_python.VendorError as error:
        raise PackageError(str(error)) from None
    manifest = json.loads(
        (python / vendor_python.MANIFEST_NAME).read_text(encoding="utf-8")
    )
    today = vendor_python.lockfile_digest(vendor_python.LOCKFILE.read_bytes())
    if manifest.get("lockfile_sha256") != today:
        raise PackageError(
            f"{python} was made from another lockfile "
            f"({str(manifest.get('lockfile_sha256'))[:12]}, today's is {today[:12]}): "
            "run `python scripts/vendor_python.py` again"
        )
    site_packages = python / vendor_python.SITE_PACKAGES
    size = sum(
        path.stat().st_size for path in site_packages.rglob("*") if path.is_file()
    )
    report.add(
        f"lockfile: {len(installed)} packages, the lockfile's ({today[:12]}); "
        f"site-packages {size / 2**20:.1f} MB"
    )
    dlls = manifest.get("vc_runtime", {}).get("dlls", {})
    report.add(
        f"Visual C++ runtime beside python.exe: {', '.join(sorted(dlls)) or 'none'}"
    )


# --------------------------------------------------------------------------
# The DLLs (section 2.3)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    name: str
    root: Path
    excluded: tuple[Path, ...] = ()
    by_name: tuple[str, ...] = ()
    never_loaded: tuple[tuple[str, str], ...] = ()
    """(file relative to the root, why): binaries whose missing imports are
    reported and do not fail the build, because nothing ever loads them."""


NEVER_LOADED_BY_POSTGRES = (
    (
        "Library/lib/pgxml.dll",
        "the xml2 contrib extension: it imports libxslt.dll, which the conda "
        "environment has on no machine (postgresql does not depend on libxslt), "
        "and the app creates no extension but vector (RFC-041 section 14)",
    ),
)
"""An extension module the server loads only on `CREATE EXTENSION xml2`.
Unloadable everywhere, and not the defect the check exists for -- a DLL found
on this machine and missing on the next. Named here, and not pruned: pruning
the PostgreSQL runtime stays out (RFC-033 section 5.1, RFC-041 section 11)."""


def runtime_scopes(resources: Path) -> list[Scope]:
    import vendor_python

    return [
        Scope(
            "python",
            resources / "runtime" / "python",
            by_name=vendor_python.CTYPES_DLLS,
        ),
        Scope(
            "postgres",
            resources / "runtime" / "postgres",
            never_loaded=NEVER_LOADED_BY_POSTGRES,
        ),
    ]


def check_dlls(scopes: Sequence[Scope], report: Report, windows: Any = None) -> None:
    from pe_imports import resolve_scope, this_windows

    windows = windows or this_windows()
    problems = []
    for scope in scopes:
        result = resolve_scope(
            scope.root, windows, excluded=scope.excluded, by_name=scope.by_name
        )
        never_loaded = dict(scope.never_loaded)
        excused = [
            missing
            for missing in result.missing
            if all(user in never_loaded for user in missing.wanted_by)
        ]
        problems += [
            f"{scope.name}: {missing.line()}"
            for missing in result.missing
            if missing not in excused
        ]
        report.add(
            f"DLLs, {scope.name}: {result.files} binaries, "
            f"{len(result.from_scope)} names found in the runtime, "
            f"{len(result.from_windows)} in Windows, "
            f"{len(result.missing) - len(excused)} missing"
        )
        for missing in excused:
            for user in missing.wanted_by:
                report.add(
                    f"DLLs, {scope.name}: {missing.name}, wanted only by {user}, "
                    f"is excused: {never_loaded[user]}"
                )
    if problems:
        raise PackageError(
            "names nothing in the package nor in Windows provides:\n  "
            + "\n  ".join(problems)
        )


# --------------------------------------------------------------------------
# The vectors (section 2.2), in the package's own interpreter
# --------------------------------------------------------------------------


def check_vectors_by_the_package(staging: Path, report: Report) -> None:
    """Run this script with `--reference` by the montagem's `python.exe`.

    `LOCALAPPDATA` is a temporary directory: the installed mode writes its
    logs and caches to the data root, and that root must not be the user's.
    """
    python = staging / "runtime" / "python" / "python.exe"
    work = Path(tempfile.mkdtemp(prefix="solidvision-vectors-"))
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("PYTHON", "SOLIDVISION"))
    }
    environment["LOCALAPPDATA"] = str(work / "Local")
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [
                str(python),
                "-X",
                "utf8",
                str(Path(__file__).resolve()),
                "--reference",
                str(REFERENCE),
                "--images",
                str(DEMO),
            ],
            cwd=work,
            env=environment,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=VECTORS_SECONDS,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0:
        tail = "\n".join((lines + completed.stderr.splitlines())[-25:])
        raise PackageError(f"the vectors, by the package's Python, failed:\n{tail}")
    for line in lines:
        report.add(f"vectors: {line.strip()}")
    report.add(f"vectors: {time.perf_counter() - started:.0f} s, the load included")


def this_machine() -> dict[str, str]:
    """The keys the reference file records, read here as the test reads them."""
    import platform

    import numpy
    import torch
    import transformers

    return {
        "cpu_count": str(os.cpu_count()),
        "numpy": numpy.__version__,
        "processor": platform.processor(),
        "python": platform.python_version(),
        "threads": str(torch.get_num_threads()),
        "torch": torch.__version__,
        "transformers": transformers.__version__,
    }


def compare_rows(found: Any, expected: Any, exact: bool) -> str:
    import numpy as np

    if exact:
        rows = [
            i
            for i in range(len(expected))
            if found[i].tobytes() != expected[i].tobytes()
        ]
        if rows:
            raise PackageError(
                f"{len(rows)} of {len(expected)} differ in their bits, first {rows[0]}"
            )
        return f"{len(expected)} vectors, bit for bit"
    largest = float(np.abs(found - expected).max())
    if largest > TOLERANCE:
        raise PackageError(f"a difference of {largest:.2e}, above {TOLERANCE:.0e}")
    return f"{len(expected)} vectors within {TOLERANCE:.0e} (largest {largest:.1e})"


def check_vectors_here(reference: Path, images: Path) -> int:
    """The vectors, in this interpreter, which must be an installation's."""
    import uuid

    import numpy as np

    from app.infrastructure.config.layout import Environment, layout

    if layout.environment is not Environment.INSTALLED:
        raise PackageError(
            f"the app at {layout.root} is a checkout: run this with the package's "
            "python.exe, which reads the package's backend"
        )
    executable = Path(sys.executable).resolve()
    if layout.root not in executable.parents:
        raise PackageError(f"{executable} is not the Python of {layout.root}")
    from app.domain.entities.image import Image
    from app.domain.value_objects.device_id import DeviceId
    from app.domain.value_objects.image_id import ImageId
    from app.domain.value_objects.image_path import ImagePath
    from app.infrastructure.ai.clip_embedding_model import (
        ClipEmbeddingModel,
        image_processor_backend,
    )
    from app.infrastructure.ai.model_runtime import read_lockfile
    from app.presentation.dependencies import get_embedding_model

    data = np.load(reference)
    if str(data["lockfile_sha256"]) != read_lockfile().sha256:
        raise PackageError("the reference was made with another models lockfile")
    recorded = json.loads(str(data["machine"]))
    machine = this_machine()
    exact = all(recorded.get(key) == value for key, value in machine.items())
    print(
        f"installed mode, {executable.parent.name}\\python.exe {machine['python']}, "
        f"torch {machine['torch']}, "
        + ("the machine of the reference" if exact else f"another machine: {machine}")
    )
    model = get_embedding_model()
    if not isinstance(model, ClipEmbeddingModel):
        raise PackageError(f"the app's model is {type(model).__name__}, not CLIP")

    def image(relative: str) -> Image:
        path = ImagePath(str(images / relative))
        return Image(
            id=ImageId(uuid.uuid5(uuid.NAMESPACE_URL, relative)),
            device_id=DeviceId(uuid.UUID(int=0)),
            relative_path=path,
            absolute_path=path,
            filename=Path(relative).stem,
            extension=Path(relative).suffix.lstrip(".").lower(),
        )

    def vectors(embeddings: Iterable[Any]) -> Any:
        return np.asarray([e.values for e in embeddings], dtype=np.float32)

    paths = [str(path) for path in data["image_paths"]]
    one_by_one = vectors(model.encode_image(image(path)) for path in paths)
    print(
        "the images one by one: "
        f"{compare_rows(one_by_one, data['image_vectors'], exact)}"
    )
    processor = model._ensure_loaded()[1]
    print(
        f"the image processor: {type(processor.image_processor).__name__}, "
        f"{image_processor_backend(processor)}"
    )
    batch = [image(path) for path in paths]
    batched = vectors(
        vector
        for start in range(0, len(batch), 8)
        for vector in model.encode_images(batch[start : start + 8])
    )
    print(
        "the images in batches of 8: "
        f"{compare_rows(batched, data['image_vectors_batched'], exact)}"
    )
    queries = [str(query) for query in data["queries"]]
    prompts = [model.build_prompt(query) for query in queries]
    expected_prompts = [str(prompt) for prompt in data["prompts"]]
    if prompts != expected_prompts:
        first = next(
            i for i, (a, b) in enumerate(zip(prompts, expected_prompts)) if a != b
        )
        raise PackageError(f"query {first} translates to {prompts[first]!r}")
    print(f"the prompts: {len(prompts)} equal, the translations included")
    found = vectors(model.encode_text(query) for query in queries)
    print(f"the queries: {compare_rows(found, data['query_vectors'], exact)}")
    return 0


# --------------------------------------------------------------------------
# The folder of electron-builder
# --------------------------------------------------------------------------


def asar_files(archive: Path) -> list[str]:
    """Every file of an `app.asar`, by its header: a Chromium pickle holding
    the JSON of the tree, after two 32-bit sizes."""
    with archive.open("rb") as handle:
        head = handle.read(16)
        if len(head) < 16:
            raise PackageError(f"{archive} is too short to be an asar archive")
        (length,) = struct.unpack_from("<I", head, 12)
        header = json.loads(handle.read(length).decode("utf-8"))
    files: list[str] = []

    def walk(node: dict[str, Any], prefix: str) -> None:
        for name, entry in node.get("files", {}).items():
            if "files" in entry:
                walk(entry, f"{prefix}{name}/")
            else:
                files.append(f"{prefix}{name}")

    walk(header, "")
    return sorted(files)


def check_asar(unpacked: Path, report: Report) -> None:
    archive = unpacked / "resources" / "app.asar"
    if not archive.is_file():
        raise PackageError(f"{unpacked} has no resources\\app.asar")
    files = asar_files(archive)
    outside = [
        name
        for name in files
        if not any(
            name == allowed or (allowed.endswith("/") and name.startswith(allowed))
            for allowed in ASAR_ALLOWED
        )
    ]
    if outside:
        raise PackageError(
            f"app.asar holds {len(outside)} files beyond the shell: "
            f"{', '.join(outside[:5])}"
        )
    for needed in (
        "dist-electron/main.js",
        "dist-electron/preload.js",
        *ASAR_ALLOWED[1:],
    ):
        if needed not in files:
            raise PackageError(f"app.asar has no {needed}")
    if (unpacked / "resources" / "app.asar.unpacked").exists():
        raise PackageError(
            "electron-builder unpacked part of the shell beside app.asar"
        )
    report.add(
        f"app.asar: {len(files)} files, {archive.stat().st_size / 1024:.0f} KB: "
        + ", ".join(files)
    )


def relative_files(
    root: Path, skip: Callable[[str], bool] = lambda _: False
) -> dict[str, int]:
    found = {}
    for directory, _, names in os.walk(root):
        for name in names:
            relative = (Path(directory) / name).relative_to(root).as_posix()
            if not skip(relative):
                found[relative] = (Path(directory) / name).stat().st_size
    return found


def check_same_resources(staging: Path, unpacked: Path, report: Report) -> None:
    """`resources\\` of the folder is the montagem, without its marker."""
    resources = unpacked / "resources"
    if (resources / STAGED_MARKER).exists():
        raise PackageError(f"{STAGED_MARKER} went into the package")
    expected = relative_files(staging, lambda name: name == STAGED_MARKER)
    found = relative_files(resources, lambda name: name.startswith("app.asar"))
    if found != expected:
        missing = sorted(set(expected) - set(found))
        extra = sorted(set(found) - set(expected))
        changed = sorted(
            name for name in set(found) & set(expected) if found[name] != expected[name]
        )
        raise PackageError(
            "resources\\ is not the montagem: "
            f"{len(missing)} missing ({', '.join(missing[:3])}), "
            f"{len(extra)} extra ({', '.join(extra[:3])}), "
            f"{len(changed)} of another size ({', '.join(changed[:3])})"
        )
    report.add(
        f"resources: the montagem's {len(found)} files, "
        f"{sum(found.values()) / 2**30:.2f} GB, without {STAGED_MARKER}"
    )


def check_unpacked(unpacked: Path, staging: Path | None, report: Report) -> None:
    resources = unpacked / "resources"
    executable = unpacked / "SolidVision.exe"
    if not executable.is_file():
        raise PackageError(f"{unpacked} has no SolidVision.exe")
    if (resources / "elevate.exe").exists():
        raise PackageError(
            "elevate.exe went into the package, and nothing here elevates"
        )
    check_asar(unpacked, report)
    if staging is not None:
        check_same_resources(staging, unpacked, report)
    check_path_budget(unpacked, INSTALL_PREFIX, report, "the installation folder")
    runtimes = runtime_scopes(resources)
    electron = Scope(
        "electron", unpacked, excluded=tuple(scope.root for scope in runtimes)
    )
    check_dlls([electron, *runtimes], report)
    size = sum(relative_files(unpacked).values())
    report.add(f"the installation folder: {size / 2**30:.2f} GB")


def check_staging(staging: Path, report: Report, vectors: bool) -> None:
    check_tree(staging, report)
    check_lockfile(staging, report)
    check_path_budget(staging, RESOURCES_PREFIX, report, "the montagem")
    check_dlls(runtime_scopes(staging), report)
    if vectors:
        check_vectors_by_the_package(staging, report)


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--staging", type=Path, help="the montagem, resources\\")
    parser.add_argument("--unpacked", type=Path, help="electron-builder's folder")
    parser.add_argument("--reference", type=Path, help="the vectors, in this Python")
    parser.add_argument("--images", type=Path, default=DEMO, help="the demo dataset")
    parser.add_argument(
        "--skip-vectors", action="store_true", help="leave the vectors out (tests)"
    )
    args = parser.parse_args(argv)
    try:
        if (
            args.reference is not None
            and args.staging is None
            and args.unpacked is None
        ):
            return check_vectors_here(args.reference.resolve(), args.images.resolve())
        if args.staging is None and args.unpacked is None:
            parser.error("say what to check: --staging, --unpacked, or --reference")
        report = Report()
        staging = args.staging.resolve() if args.staging is not None else None
        if args.unpacked is not None:
            # The folder's checks, against the montagem when it is named.
            print(f"the folder: {args.unpacked.resolve()}", flush=True)
            check_unpacked(args.unpacked.resolve(), staging, report)
        elif staging is not None:
            print(f"the montagem: {staging}", flush=True)
            check_staging(staging, report, vectors=not args.skip_vectors)
        print(f"check_package: {len(report.facts)} checks passed")
        return 0
    except PackageError as error:
        print(f"check_package: FAILED: {error}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
