"""What loading the two models does on the network today, before RFC-035 (pilot).

RFC-035 was written before its implementation, and section 2 of it rests on a
reading of the installed `transformers` and `huggingface_hub`, not on anything
observed. Two of its claims are hypotheses that this pilot turns into facts or
kills:

    section 2.2  the `refs/pr/6` snapshot of the translator in the local cache
                 was downloaded by `transformers`' own background thread,
                 `Thread-auto_conversion`, when the `.bin` was loaded
    section 2.4  loading by name asks the Hub which commit `main` is, on every
                 load, even with every file already in the cache

and a third, which section 5.1 calls the first layer:

    section 5.1  `from_pretrained(<a local directory>)` makes no request at all

Four scenarios, each in its own subprocess with its own empty `HF_HOME` and
`TEMP`, so the constants `huggingface_hub` reads at import (RFC-035 2.3) are
fresh every time and the user's real cache is never read or written:

    translator-empty-cache  the translator by name, nothing cached, online
    translator-full-cache   the same, again, over the cache the first one left
    clip-full-cache         CLIP by name, over a copy of the real CLIP cache
    local-directories       both models from snapshot directories, online,
                            with no offline flag and no `local_files_only`

The loads mirror the four calls of `clip_embedding_model.py` and
`query_translator.py` exactly -- `from_pretrained(name)`, no other argument --
without importing the app, so nothing is written to the app's data root.

Every request that goes through `httpx`, the client `huggingface_hub` uses, is
recorded: method, host, path (never the query string, which carries signed
download tokens), status and thread. Bytes that `hf_xet` moves in Rust do not
go through `httpx`; the metadata calls around them do, and the files they
leave are listed from the disk.

**No external side effect.** When no conversion pull request exists,
`transformers` asks a remote service to open one on the model's repository
(`spawn_conversion`). The children replace that function with a stub that only
records the call. Every other request is a read.

Run it with the project's virtualenv, with the output redirected next to this
script (`*.log` is outside git, like every experiment's log):

    python experiments/rfc-035-offline-models/pilot_hub_access_probe.py --work-dir DIR > LOG 2>&1

`DIR` must not exist; it is created, holds roughly 1.2 GB during the run, and
is deleted at the end unless `--keep` is given.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

TRANSLATOR = "Helsinki-NLP/opus-mt-ROMANCE-en"
CLIP = "laion/CLIP-ViT-B-32-laion2B-s34B-b79K"
REAL_HUB = Path.home() / ".cache" / "huggingface" / "hub"
REAL_MODEL_DIRECTORIES = [
    REAL_HUB / "models--Helsinki-NLP--opus-mt-ROMANCE-en",
    REAL_HUB / "models--laion--CLIP-ViT-B-32-laion2B-s34B-b79K",
]
CHILD_TIMEOUT_SECONDS = 1200
CONVERSION_THREAD_TIMEOUT_SECONDS = 300
LIBRARY_PREFIXES = ("HF_", "HUGGINGFACE_", "TRANSFORMERS_", "TORCHINDUCTOR_")
LOG_MARKERS = (
    "loading weights file",
    "offline",
    "safetensors",
    "conversion",
    "pull request",
)


# --------------------------------------------------------------------------
# The child: one load, everything it does on the network, as one JSON line.
# --------------------------------------------------------------------------


def child(scenario: str, spec: dict[str, str]) -> None:
    import logging
    import threading

    import httpx

    requests: list[dict[str, object]] = []
    threads: list[str] = []
    conversion_calls: list[dict[str, object]] = []
    log_lines: list[str] = []

    original_send = httpx.Client.send

    def recording_send(self: httpx.Client, request: httpx.Request, *args, **kwargs):  # type: ignore[no-untyped-def]
        started = time.perf_counter()
        entry: dict[str, object] = {
            "method": request.method,
            "host": request.url.host,
            "path": request.url.path,
            "thread": threading.current_thread().name,
        }
        try:
            response = original_send(self, request, *args, **kwargs)
        except Exception as error:  # noqa: BLE001 -- recorded and re-raised
            entry["error"] = type(error).__name__
            entry["ms"] = round((time.perf_counter() - started) * 1000, 1)
            requests.append(entry)
            raise
        entry["status"] = response.status_code
        entry["ms"] = round((time.perf_counter() - started) * 1000, 1)
        requests.append(entry)
        return response

    httpx.Client.send = recording_send  # type: ignore[method-assign]

    original_start = threading.Thread.start

    def recording_start(self: threading.Thread) -> None:
        threads.append(self.name)
        original_start(self)

    threading.Thread.start = recording_start  # type: ignore[method-assign]

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            message = record.getMessage()
            if any(marker in message.lower() for marker in LOG_MARKERS):
                log_lines.append(f"{record.name}: {message}")

    import transformers
    import transformers.safetensors_conversion as conversion

    transformers.logging.set_verbosity_info()
    transformers.logging.get_logger().addHandler(Collect())

    def stub_spawn_conversion(token: object, private: object, model_id: str) -> None:
        conversion_calls.append({"model_id": model_id, "private": bool(private)})

    conversion.spawn_conversion = stub_spawn_conversion  # type: ignore[assignment]

    import huggingface_hub
    import huggingface_hub.constants

    result: dict[str, object] = {
        "scenario": scenario,
        "offline_mode": huggingface_hub.is_offline_mode(),
        "hf_home": huggingface_hub.constants.HF_HOME,
    }

    loads: list[dict[str, object]] = []
    started = time.perf_counter()
    try:
        if scenario.startswith("translator"):
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

            model = AutoModelForSeq2SeqLM.from_pretrained(TRANSLATOR)
            model.eval()
            AutoTokenizer.from_pretrained(TRANSLATOR)
            loads.append(_describe(TRANSLATOR, model))
        elif scenario == "clip-full-cache":
            from transformers import AutoProcessor, CLIPModel

            model = CLIPModel.from_pretrained(CLIP)
            model.eval()
            AutoProcessor.from_pretrained(CLIP)
            loads.append(_describe(CLIP, model))
        elif scenario == "local-directories":
            from transformers import (
                AutoModelForSeq2SeqLM,
                AutoProcessor,
                AutoTokenizer,
                CLIPModel,
            )

            translator = AutoModelForSeq2SeqLM.from_pretrained(spec["translator"])
            translator.eval()
            AutoTokenizer.from_pretrained(spec["translator"])
            loads.append(_describe(spec["translator"], translator))
            clip = CLIPModel.from_pretrained(spec["clip"])
            clip.eval()
            AutoProcessor.from_pretrained(spec["clip"])
            loads.append(_describe(spec["clip"], clip))
        else:
            raise ValueError(f"unknown scenario {scenario}")
        result["load_seconds"] = round(time.perf_counter() - started, 2)
    except Exception as error:  # noqa: BLE001 -- the failure is the result
        result["load_seconds"] = round(time.perf_counter() - started, 2)
        result["error"] = f"{type(error).__name__}: {str(error).splitlines()[0][:300]}"

    waited = time.perf_counter()
    pending = [
        thread
        for thread in threading.enumerate()
        if thread.name == "Thread-auto_conversion"
    ]
    for thread in pending:
        thread.join(timeout=CONVERSION_THREAD_TIMEOUT_SECONDS)
    result["conversion_threads_alive_after_wait"] = sum(
        thread.is_alive() for thread in pending
    )
    result["conversion_wait_seconds"] = round(time.perf_counter() - waited, 2)

    result["loads"] = loads
    result["requests"] = requests
    result["threads"] = threads
    result["spawn_conversion_calls"] = conversion_calls
    result["log"] = log_lines
    result["hf_xet_imported"] = "hf_xet" in sys.modules
    print("RESULT " + json.dumps(result), flush=True)


def _describe(source: str, model: object) -> dict[str, object]:
    config = getattr(model, "config", None)
    return {
        "source": source,
        "class": type(model).__name__,
        "commit_hash": getattr(config, "_commit_hash", None),
        "name_or_path": getattr(config, "_name_or_path", None),
    }


# --------------------------------------------------------------------------
# The orchestrator.
# --------------------------------------------------------------------------


def fingerprint(directory: Path) -> dict[str, tuple[int, int]]:
    """Every file under `directory`: size and modification time, by relative path."""
    if not directory.exists():
        return {}
    return {
        str(path.relative_to(directory)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def describe_cache(hf_home: Path) -> list[str]:
    lines: list[str] = []
    hub = hf_home / "hub"
    if not hub.exists():
        return ["    (no hub directory)"]
    for repository in sorted(hub.glob("models--*")):
        lines.append(f"    {repository.name}")
        for ref in sorted((repository / "refs").rglob("*")):
            if ref.is_file():
                lines.append(
                    f"      ref {ref.relative_to(repository / 'refs').as_posix()}"
                    f" -> {ref.read_text().strip()}"
                )
        for snapshot in sorted((repository / "snapshots").glob("*")):
            files = sorted(p for p in snapshot.rglob("*") if p.is_file() or p.is_symlink())
            total = sum(p.stat().st_size for p in files if p.exists())
            lines.append(f"      snapshot {snapshot.name}: {len(files)} files, {total:,} bytes")
            for path in files:
                kind = "link" if path.is_symlink() else "file"
                size = path.stat().st_size if path.exists() else -1
                lines.append(f"        {kind} {path.name} {size:,}")
        blobs = list((repository / "blobs").glob("*")) if (repository / "blobs").exists() else []
        lines.append(f"      blobs: {len(blobs)} entries")
        no_exist = sorted(
            f"{p.parent.name[:8]}/{p.name}"
            for p in (repository / ".no_exist").rglob("*")
            if p.is_file()
        )
        lines.append(f"      .no_exist: {', '.join(no_exist) if no_exist else '(none)'}")
    xet = hf_home / "xet"
    if xet.exists():
        xet_bytes = sum(p.stat().st_size for p in xet.rglob("*") if p.is_file())
        lines.append(f"    xet cache: {xet_bytes:,} bytes")
    return lines


def child_environment(hf_home: Path, temp: Path) -> tuple[dict[str, str], list[str]]:
    environment = dict(os.environ)
    removed = sorted(name for name in environment if name.startswith(LIBRARY_PREFIXES))
    for name in removed:
        del environment[name]
    environment["HF_HOME"] = str(hf_home)
    environment["TEMP"] = str(temp)
    environment["TMP"] = str(temp)
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment, removed


def run_scenario(
    work: Path, scenario: str, hf_home: Path, spec: dict[str, str] | None = None
) -> dict[str, object]:
    temp = work / f"temp-{scenario}"
    temp.mkdir(parents=True)
    hf_home.mkdir(parents=True, exist_ok=True)
    environment, _ = child_environment(hf_home, temp)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--child",
        scenario,
        "--spec",
        json.dumps(spec or {}),
    ]
    print(f"\n=== {scenario} ===", flush=True)
    print(f"  HF_HOME = <work>\\{hf_home.relative_to(work)}", flush=True)
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            env=environment,
            cwd=str(work),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=CHILD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(f"  TIMEOUT after {CHILD_TIMEOUT_SECONDS} s", flush=True)
        return {"scenario": scenario, "error": "timeout"}
    elapsed = time.perf_counter() - started
    result_line = next(
        (line for line in completed.stdout.splitlines() if line.startswith("RESULT ")),
        None,
    )
    if result_line is None:
        print(f"  child exited {completed.returncode} without a result", flush=True)
        print("  stderr tail:", flush=True)
        for line in completed.stderr.splitlines()[-15:]:
            print(f"    {line}", flush=True)
        return {"scenario": scenario, "error": "no result"}
    result: dict[str, object] = json.loads(result_line[len("RESULT ") :])
    report(result, elapsed)
    print("  cache afterwards:", flush=True)
    for line in describe_cache(hf_home):
        print(line, flush=True)
    leftovers = sorted(p.name for p in temp.iterdir())
    print(f"  TEMP afterwards: {', '.join(leftovers) if leftovers else '(empty)'}", flush=True)
    return result


def report(result: dict[str, object], elapsed: float) -> None:
    print(f"  process: {elapsed:.1f} s; load: {result.get('load_seconds')} s", flush=True)
    print(f"  is_offline_mode(): {result['offline_mode']}", flush=True)
    if "error" in result:
        print(f"  LOAD FAILED: {result['error']}", flush=True)
    for load in result.get("loads", []):  # type: ignore[union-attr]
        print(
            f"  loaded {load['class']} from {load['source']}; "
            f"_commit_hash={load['commit_hash']}",
            flush=True,
        )
    requests = result.get("requests", [])
    assert isinstance(requests, list)
    by_thread: dict[str, int] = {}
    for request in requests:
        by_thread[request["thread"]] = by_thread.get(request["thread"], 0) + 1
    print(
        f"  HTTP requests through httpx: {len(requests)}"
        + (f" ({', '.join(f'{k}: {v}' for k, v in sorted(by_thread.items()))})" if by_thread else ""),
        flush=True,
    )
    for request in requests:
        outcome = request.get("status", request.get("error"))
        print(
            f"    [{request['thread']}] {request['method']} {request['host']}"
            f"{request['path']} -> {outcome} ({request['ms']} ms)",
            flush=True,
        )
    threads = result.get("threads", [])
    print(f"  threads started: {', '.join(threads) if threads else '(none)'}", flush=True)  # type: ignore[arg-type]
    print(
        f"  auto_conversion threads still alive after waiting: "
        f"{result.get('conversion_threads_alive_after_wait')} "
        f"(waited {result.get('conversion_wait_seconds')} s)",
        flush=True,
    )
    calls = result.get("spawn_conversion_calls", [])
    print(
        f"  spawn_conversion called (stubbed, nothing sent): {len(calls)}",  # type: ignore[arg-type]
        flush=True,
    )
    print(f"  hf_xet imported: {result.get('hf_xet_imported')}", flush=True)
    log = result.get("log", [])
    if log:
        print("  transformers log (filtered):", flush=True)
        for line in log:  # type: ignore[union-attr]
            print(f"    {line}", flush=True)


def snapshot_directory(hf_home: Path, repository: str) -> Path | None:
    folder = hf_home / "hub" / ("models--" + repository.replace("/", "--"))
    main = folder / "refs" / "main"
    if not main.exists():
        return None
    snapshot = folder / "snapshots" / main.read_text().strip()
    return snapshot if snapshot.exists() else None


def orchestrate(work: Path, keep: bool) -> int:
    print("# RFC-035 pilot: what loading the two models does on the network today")
    print(f"date: {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}")
    print(f"machine: {platform.platform()}, {platform.processor()}")
    print(f"python: {sys.version.split()[0]} ({sys.executable})")
    for package in ("torch", "transformers", "huggingface-hub", "httpx", "hf-xet", "safetensors"):
        try:
            print(f"  {package} {importlib.metadata.version(package)}")
        except importlib.metadata.PackageNotFoundError:
            print(f"  {package} (not installed)")
    _, removed = child_environment(work, work)
    print(
        "library variables in this environment, removed from every child (names only): "
        + (", ".join(removed) if removed else "(none)")
    )
    print(f"HF_TOKEN set: {'HF_TOKEN' in os.environ}")

    before = {str(d): fingerprint(d) for d in REAL_MODEL_DIRECTORIES}
    hub_listing_before = sorted(p.name for p in REAL_HUB.iterdir()) if REAL_HUB.exists() else []
    for directory, files in before.items():
        print(f"real cache before: {Path(directory).name}: {len(files)} files")

    work.mkdir(parents=True)
    results: list[dict[str, object]] = []

    translator_home = work / "hf-translator"
    results.append(run_scenario(work, "translator-empty-cache", translator_home))
    results.append(run_scenario(work, "translator-full-cache", translator_home))

    clip_home = work / "hf-clip"
    clip_source = REAL_MODEL_DIRECTORIES[1]
    copy_started = time.perf_counter()
    shutil.copytree(clip_source, clip_home / "hub" / clip_source.name)
    print(
        f"\n(copied the real CLIP cache directory into <work>\\hf-clip in "
        f"{time.perf_counter() - copy_started:.1f} s; refs, snapshots, blobs and .no_exist as they are)"
    )
    results.append(run_scenario(work, "clip-full-cache", clip_home))

    translator_snapshot = snapshot_directory(translator_home, TRANSLATOR)
    clip_snapshot = snapshot_directory(clip_home, CLIP)
    if translator_snapshot is None or clip_snapshot is None:
        print("\n=== local-directories ===\n  skipped: a snapshot directory is missing")
    else:
        results.append(
            run_scenario(
                work,
                "local-directories",
                work / "hf-local",
                {"translator": str(translator_snapshot), "clip": str(clip_snapshot)},
            )
        )

    after = {str(d): fingerprint(d) for d in REAL_MODEL_DIRECTORIES}
    hub_listing_after = sorted(p.name for p in REAL_HUB.iterdir()) if REAL_HUB.exists() else []
    print("\n=== the real cache ===")
    untouched = after == before and hub_listing_after == hub_listing_before
    for directory in REAL_MODEL_DIRECTORIES:
        same = after[str(directory)] == before[str(directory)]
        print(f"  {directory.name}: {'unchanged' if same else 'CHANGED'}")
    print(f"  hub listing: {'unchanged' if hub_listing_after == hub_listing_before else 'CHANGED'}")
    print(f"  untouched: {untouched}")

    print("\n=== summary ===")
    for result in results:
        requests = result.get("requests", [])
        count = len(requests) if isinstance(requests, list) else "?"
        print(
            f"  {result['scenario']}: {count} requests, "
            f"auto_conversion thread: {'Thread-auto_conversion' in result.get('threads', [])}, "  # type: ignore[operator]
            f"spawn_conversion calls: {len(result.get('spawn_conversion_calls', []))}, "  # type: ignore[arg-type]
            f"error: {result.get('error', '-')}"
        )

    if keep:
        print(f"\nwork directory kept: {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
        print(f"\nwork directory removed: {not work.exists()}")
    return 0 if untouched else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--child")
    parser.add_argument("--spec", default="{}")
    arguments = parser.parse_args()
    if arguments.child:
        child(arguments.child, json.loads(arguments.spec))
        return 0
    if arguments.work_dir is None:
        parser.error("--work-dir is required")
    if arguments.work_dir.exists():
        parser.error(f"{arguments.work_dir} already exists")
    return orchestrate(arguments.work_dir.resolve(), arguments.keep)


if __name__ == "__main__":
    sys.exit(main())
