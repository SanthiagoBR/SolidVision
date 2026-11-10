"""RFC-050 section 2 pilot: what the two new things to keep cost here.

    .venv\\Scripts\\python.exe experiments/rfc-050-suite-do-backend/pilot_caches.py

1. `runtime\\postgres`: `vendor_postgres.py` into a new folder (downloads
   from conda-forge with empty caches), then the folder packed with
   `tar --zstd`, the format of `actions/cache`, removed and unpacked.
2. The base Python 3.12.9 from nuget.org, the package of RFC-048 section
   17.4: downloaded, its SHA-512 checked against the one nuget.org
   publishes, unpacked with `tar`, and a venv made from it.

Writes `pilot_caches.json` beside this file. Nothing outside a temporary
folder is touched.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
TAR = r"C:\Windows\System32\tar.exe"

NUGET_VERSION = "3.12.9"
NUGET_PACKAGE = (
    "https://api.nuget.org/v3-flatcontainer/python/"
    f"{NUGET_VERSION}/python.{NUGET_VERSION}.nupkg"
)
NUGET_REGISTRATION = (
    "https://api.nuget.org/v3/registration5-semver1/python/"
    f"{NUGET_VERSION}.json"
)


def folder_size(path: Path) -> tuple[int, int]:
    files = [p for p in path.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


def timed(command: list[str], cwd: Path | None = None) -> float:
    started = time.perf_counter()
    subprocess.run(command, cwd=cwd, check=True, stdin=subprocess.DEVNULL)
    return round(time.perf_counter() - started, 1)


def pack_round_trip(folder: Path, work: Path, name: str) -> dict[str, Any]:
    archive = work / f"{name}.tar.zst"
    files, size = folder_size(folder)
    result: dict[str, Any] = {"files": files, "megabytes": round(size / 1e6, 1)}
    result["pack_seconds"] = timed(
        [TAR, "--zstd", "-cf", str(archive), "-C", str(folder.parent), folder.name]
    )
    result["archive_megabytes"] = round(archive.stat().st_size / 1e6, 1)
    started = time.perf_counter()
    shutil.rmtree(folder)
    result["remove_seconds"] = round(time.perf_counter() - started, 1)
    result["unpack_seconds"] = timed(
        [TAR, "--zstd", "-xf", str(archive), "-C", str(folder.parent)]
    )
    assert folder_size(folder) == (files, size)
    return result


def postgres(work: Path) -> dict[str, Any]:
    target = work / "postgres"
    result = {
        "vendor_seconds": timed(
            [
                sys.executable,
                str(REPO / "scripts" / "vendor_postgres.py"),
                "--target",
                str(target),
            ]
        )
    }
    return result | pack_round_trip(target, work, "postgres")


def nuget(work: Path) -> dict[str, Any]:
    package = work / "python.nupkg"
    started = time.perf_counter()
    urllib.request.urlretrieve(NUGET_PACKAGE, package)
    result: dict[str, Any] = {
        "download_seconds": round(time.perf_counter() - started, 1),
        "package_megabytes": round(package.stat().st_size / 1e6, 1),
    }
    digest = hashlib.sha512(package.read_bytes()).digest()
    request = urllib.request.Request(NUGET_REGISTRATION)
    with urllib.request.urlopen(request, timeout=60) as response:
        registration = json.load(response)
    published = registration.get("catalogEntry")
    if isinstance(published, str):
        with urllib.request.urlopen(published, timeout=60) as response:
            published = json.load(response)
    expected = published.get("packageHash") if isinstance(published, dict) else None
    result["sha512_matches_nuget_org"] = (
        expected is not None and base64.b64decode(expected) == digest
    )
    result["sha256"] = hashlib.sha256(package.read_bytes()).hexdigest()
    unpacked = work / "nuget"
    unpacked.mkdir()
    result["unpack_seconds"] = timed([TAR, "-xf", str(package), "-C", str(unpacked)])
    python = unpacked / "tools" / "python.exe"
    files, size = folder_size(unpacked / "tools")
    result |= {"tools_files": files, "tools_megabytes": round(size / 1e6, 1)}
    result["version"] = subprocess.run(
        [str(python), "-c", "import platform; print(platform.python_version())"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    result["venv_seconds"] = timed([str(python), "-m", "venv", str(work / "venv")])
    result["tools"] = pack_round_trip(unpacked / "tools", work, "tools")
    return result


PARTS = {"postgres": postgres, "nuget": nuget}


def main() -> int:
    """`pilot_caches.py [postgres] [nuget]`: each part apart, so that one
    that fails (a download, say) keeps what the other measured."""
    out = HERE / "pilot_caches.json"
    results = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    for name in sys.argv[1:] or list(PARTS):
        work = Path(tempfile.mkdtemp(prefix="rfc050-caches-"))
        try:
            results[name] = PARTS[name](work)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
