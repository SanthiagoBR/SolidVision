"""Materialize the embedded PostgreSQL runtime from the lockfile (RFC-033 5.1).

RFC-033a pinned three packages -- `postgresql`, `pgvector` and `libpq` -- by
version and build, and left the other thirteen of the environment (OpenSSL,
ICU, krb5, zlib, the Visual C++ runtime...) pinned only in a log. A second
materialization a month later could resolve another OpenSSL. So the source is
now the explicit environment itself, `scripts/postgres-win-64.lock`: the output
of `micromamba list --explicit --md5`, every package's URL with its md5, in git.

    python scripts/vendor_postgres.py            # runtime/postgres, from the lock
    python scripts/vendor_postgres.py --lock     # solve the pins, rewrite the lock,
                                                 # then materialize from it

What it does, in order:

1. Downloads micromamba 2.9.0 from conda-forge and checks the sha256 RFC-033a
   pinned. Nothing is installed: the binary lives in a temporary directory.
2. Creates the environment **from the lockfile, without a solver**, into a
   temporary sibling of the target, with a package cache of its own -- nothing
   lands in the user's conda caches.
3. Checks the md5 of every downloaded archive against the lockfile, and the
   versions of `postgresql` and `pgvector` against what the lock says.
4. Removes the `.pdb` files, as RFC-033a did, and nothing else: further
   pruning belongs to the installer (RFC-033 5.1).
5. Writes `SOLIDVISION-RUNTIME.json`, the manifest the adapter refuses to run
   without, and only then renames the directory into place. A run killed half
   way leaves a `.vendor-tmp` directory, never a runtime that looks complete.

`runtime/` is outside git (`.gitignore`): 123 MB the lockfile reproduces byte
for byte, and the directory the installer will copy from.

No new dependency: download, tar, hashlib and subprocess are the standard
library's.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOCKFILE = REPO / "scripts" / "postgres-win-64.lock"
DEFAULT_TARGET = REPO / "runtime" / "postgres"
MANIFEST_NAME = "SOLIDVISION-RUNTIME.json"
MANIFEST_FORMAT = 1

MICROMAMBA_VERSION = "2.9.0"
MICROMAMBA_URL = (
    "https://api.anaconda.org/download/conda-forge/micromamba/"
    f"{MICROMAMBA_VERSION}/win-64/micromamba-{MICROMAMBA_VERSION}-0.tar.bz2"
)
MICROMAMBA_SHA256 = "97a336f4ab794bd96a6a4da5e6ed63e75a1d31830414a182419b23d3b36f3fe0"
"""RFC-033a section 4.1, step 1: conda-forge's own listing for this file."""
MICROMAMBA_MEMBER = "Library/bin/micromamba.exe"

CHANNEL = "conda-forge"
SUBDIR = "win-64"
PINNED = {
    "postgresql": ("16.15", "he837cf3_0"),
    "pgvector": ("0.8.6", "h2466b09_0"),
    "libpq": ("16.15", "h43e12c5_0"),
}
"""RFC-033a's three pins. Used only by `--lock`, to solve the environment once."""

REQUIRED = (
    "Library/bin/postgres.exe",
    "Library/bin/initdb.exe",
    "Library/bin/pg_ctl.exe",
    "Library/lib/vector.dll",
)
"""What the adapter checks on every start (RFC-033 5.2); checked here first."""


class VendorError(Exception):
    """A step failed. The message says which; nothing half-built is left in place."""


def mb(size: int) -> float:
    return size / (1024 * 1024)


def dir_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def digest(path: Path, algorithm: str) -> str:
    hasher = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def executables_running_from(runtime: Path) -> list[str]:
    """The programs of an existing runtime that are executing right now.

    Windows lets a directory be renamed, and most of it deleted, with a
    server running from inside it -- measured: the rename succeeded, and the
    cluster was left running from a runtime that had no name any more. What
    Windows does not allow is opening a running image for writing, so that is
    the question asked, before anything is moved.
    """
    running = []
    for executable in sorted((runtime / "Library" / "bin").glob("*.exe")):
        try:
            with executable.open("r+b"):
                pass
        except PermissionError:
            running.append(executable.name)
    return running


def lockfile_digest() -> str:
    """The lockfile's sha256 with line endings normalized.

    Git checks the file out with CRLF or LF depending on `core.autocrlf`, and
    the manifest should name the same lockfile on every machine.
    """
    content = LOCKFILE.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(content).hexdigest()


def run(args: list[str | Path], env: dict[str, str]) -> str:
    completed = subprocess.run(
        [str(arg) for arg in args],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=1800,
    )
    if completed.returncode != 0:
        tail = "\n".join((completed.stdout + completed.stderr).splitlines()[-25:])
        raise VendorError(f"{Path(str(args[0])).name} {args[1]} failed:\n{tail}")
    return completed.stdout


def fetch_micromamba(work: Path) -> Path:
    archive = work / MICROMAMBA_URL.rsplit("/", 1)[1]
    request = urllib.request.Request(
        MICROMAMBA_URL, headers={"User-Agent": "SolidVision vendor_postgres"}
    )
    with (
        urllib.request.urlopen(request, timeout=300) as response,
        archive.open("wb") as handle,
    ):
        shutil.copyfileobj(response, handle)
    found = digest(archive, "sha256")
    if found != MICROMAMBA_SHA256:
        raise VendorError(f"micromamba sha256 {found}, pinned {MICROMAMBA_SHA256}")
    executable = work / "micromamba.exe"
    with tarfile.open(archive, "r:bz2") as tar:
        member = tar.getmember(MICROMAMBA_MEMBER)
        member.name = executable.name
        tar.extract(member, work, filter="data")
    archive.unlink()
    print(f"micromamba {MICROMAMBA_VERSION}: sha256 OK")
    return executable


def mamba_env(work: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        MAMBA_ROOT_PREFIX=str(work / "mamba-root"),
        CONDA_PKGS_DIRS=str(work / "pkgs"),
        MAMBA_NO_BANNER="1",
    )
    return env


def read_lock(lockfile: Path) -> dict[str, str]:
    """URL -> md5, from the `URL#md5` lines of an explicit environment."""
    entries: dict[str, str] = {}
    for line in lockfile.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("http"):
            continue
        url, _, md5 = line.partition("#")
        if len(md5) != 32:
            raise VendorError(f"{lockfile.name}: no md5 on {url}")
        entries[url] = md5
    if not entries:
        raise VendorError(f"{lockfile.name} lists no packages")
    return entries


def relock(micromamba: Path, work: Path) -> None:
    """Solve RFC-033a's three pins once and write what the solver chose."""
    solved = work / "solved"
    specs = [f"{name}={ver}={build}" for name, (ver, build) in PINNED.items()]
    run(
        [
            micromamba,
            "create",
            "--yes",
            "--no-rc",
            "--prefix",
            solved,
            "--override-channels",
            "--channel",
            CHANNEL,
            "--platform",
            SUBDIR,
            *specs,
        ],
        mamba_env(work),
    )
    explicit = run(
        [micromamba, "list", "--prefix", solved, "--explicit", "--md5"],
        mamba_env(work),
    )
    # micromamba 2.9.0 prints a title line and the URLs, without conda's
    # `@EXPLICIT` marker; the header below supplies it.
    packages = [
        line.strip() for line in explicit.splitlines() if line.startswith("http")
    ]
    if not packages:
        raise VendorError("micromamba list --explicit --md5 gave no packages")
    header = [
        "# The embedded PostgreSQL runtime, as an explicit environment (RFC-033 5.1).",
        "# Generated by `python scripts/vendor_postgres.py --lock` on "
        f"{datetime.date.today().isoformat()}",
        f"# from {' '.join(specs)} ({CHANNEL}, {SUBDIR}, micromamba "
        f"{MICROMAMBA_VERSION}).",
        "# Materialized without a solver by `python scripts/vendor_postgres.py`.",
        f"# platform: {SUBDIR}",
        "@EXPLICIT",
    ]
    LOCKFILE.write_text("\n".join([*header, *packages]) + "\n", encoding="utf-8")
    shutil.rmtree(solved, ignore_errors=True)
    print(f"{LOCKFILE.relative_to(REPO)}: {len(packages)} packages (re-solved)")


def materialize(micromamba: Path, work: Path, staging: Path) -> dict[str, str]:
    lock = read_lock(LOCKFILE)
    run(
        [
            micromamba,
            "create",
            "--yes",
            "--no-rc",
            "--prefix",
            staging,
            "--file",
            LOCKFILE,
        ],
        mamba_env(work),
    )
    # The md5 the lock carries, checked here rather than trusted to the tool:
    # the archive in the cache is the thing that was downloaded. micromamba
    # 2.9.0 caches an explicit URL under `pkgs/https/<host>/<channel>/<subdir>/`,
    # so the archive is found by name rather than by an assumed layout.
    for url, md5 in lock.items():
        name = url.rsplit("/", 1)[1]
        archive = next((work / "pkgs").rglob(name), None)
        if archive is None or not archive.is_file():
            raise VendorError(f"{name} was not downloaded")
        found = digest(archive, "md5")
        if found != md5:
            raise VendorError(f"{archive.name}: md5 {found}, lockfile says {md5}")
    print(f"{len(lock)} packages from {LOCKFILE.name}: md5 OK")
    return lock


def installed_version(prefix: Path, name: str) -> str:
    metas = list((prefix / "conda-meta").glob(f"{name}-[0-9]*.json"))
    if len(metas) != 1:
        raise VendorError(f"{name}: {len(metas)} records in conda-meta")
    record = json.loads(metas[0].read_text(encoding="utf-8"))
    return str(record["version"])


def prune(prefix: Path) -> dict[str, int]:
    before = dir_size(prefix)
    symbols = list(prefix.rglob("*.pdb"))
    removed = sum(path.stat().st_size for path in symbols)
    for path in symbols:
        path.unlink()
    after = dir_size(prefix)
    print(
        f"pruned {len(symbols)} .pdb files ({mb(removed):.0f} MB): "
        f"{mb(before):.0f} MB -> {mb(after):.0f} MB"
    )
    return {"before": before, "pdb_files": len(symbols), "after": after}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--target",
        type=Path,
        default=DEFAULT_TARGET,
        help=f"where the runtime goes (default: {DEFAULT_TARGET})",
    )
    parser.add_argument(
        "--lock",
        action="store_true",
        help="solve RFC-033a's three pins and rewrite the lockfile first",
    )
    args = parser.parse_args()
    if sys.platform != "win32":
        print("The runtime is win-64; materialize it on Windows.")
        return 2

    target: Path = args.target.resolve()
    staging = target.with_name(target.name + ".vendor-tmp")
    work = Path(tempfile.mkdtemp(prefix="solidvision-vendor-"))
    try:
        micromamba = fetch_micromamba(work)
        if args.lock:
            relock(micromamba, work)
        elif not LOCKFILE.is_file():
            raise VendorError(f"{LOCKFILE} is missing: run with --lock to create it")
        shutil.rmtree(staging, ignore_errors=True)
        if staging.exists():
            raise VendorError(f"{staging} is left over and could not be removed")
        staging.parent.mkdir(parents=True, exist_ok=True)
        lock = materialize(micromamba, work, staging)
        for relative in REQUIRED:
            if not (staging / relative).is_file():
                raise VendorError(f"{relative} is not in the environment")
        postgresql = installed_version(staging, "postgresql")
        pgvector = installed_version(staging, "pgvector")
        if (postgresql, pgvector) != (PINNED["postgresql"][0], PINNED["pgvector"][0]):
            raise VendorError(f"the lock gives postgresql {postgresql}, {pgvector}")
        sizes = prune(staging)
        manifest = {
            "format": MANIFEST_FORMAT,
            "postgresql": postgresql,
            "pgvector": pgvector,
            "lockfile": LOCKFILE.name,
            "lockfile_sha256": lockfile_digest(),
            "packages": len(lock),
            "materialized_at": datetime.datetime.now()
            .astimezone()
            .isoformat(timespec="seconds"),
            "pruned_bytes": sizes["after"],
            "pdb_files_removed": sizes["pdb_files"],
        }
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        running = executables_running_from(target)
        if running:
            raise VendorError(
                f"{target} is in use: {', '.join(running)} running from it. Stop "
                "the cluster (`python -m app.infrastructure.database.embedded "
                "stop`) and run this again. Nothing was changed; the new runtime "
                f"is waiting in {staging}."
            )
        retired = target.with_name(target.name + ".vendor-old")
        shutil.rmtree(retired, ignore_errors=True)
        if target.exists():
            target.rename(retired)
        staging.rename(target)
        shutil.rmtree(retired, ignore_errors=True)
        size = f"{mb(sizes['after']):.0f} MB"
        print(f"runtime: {target}")
        print(f"  postgresql {postgresql}, pgvector {pgvector}, {size}")
        return 0
    except (VendorError, OSError) as error:
        print(f"FAILED: {error}")
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
