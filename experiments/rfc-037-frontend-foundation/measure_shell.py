r"""The window, measured (RFC-037 section 10).

    python measure_shell.py > measure_shell.log 2>&1

    --list             the checks
    --only a,b         just these (in the order of --list)
    --work PATH        the work directory (default: a temporary one)
    --keep             keep it
    --shots PATH       where the screenshots go (default: the work directory)
    --quiet-seconds N  how long `quiet` keeps the home screen open (600)

Every window is `frontend\node_modules\electron\dist\electron.exe frontend`,
over the build -- `npm run build` first -- in this checkout, with
`DATABASE_SOURCE=embedded` and a `LOCALAPPDATA` of its own under the work
directory: the data root, the cluster, the logs and `shell.log` are the work
directory's, and the developer's `SolidVision-dev` is not touched. The Chromium
profile is: Electron asks Windows for the roaming folder, whatever the
environment says, and it is the profile of a developer's own `npm start`.

**Every window paints, whatever is in front of it.** The windows are started with
`--disable-features=CalculateNativeWinOcclusion`, a switch of Chromium's, not of
the app. Without it, a window that opens behind the editor this script runs from
-- Windows does not let a background process bring one to the front -- is taken
by Chromium for a hidden one: it paints nothing, runs no animation frame, and its
page polls nothing. The first run of this script waited 133 and 168 s for the
mark of the home screen, which waits on a frame, until somebody brought the
window up; a user who opens the app is looking at it.

Each window is closed the way a click on its X closes it: `WM_CLOSE`, posted to
it. The numbers come from the marks of `shell.log` (section 6.5), written with
this machine's clock, against the moment this script created the process. While
a window is open, the process tree under it and every visible window are
sampled every 100 ms: a console window of the tree, or any console window that
was not there before, fails the opening (section 2.7).

**What is not measured here:** the logoff (RFC-036 section 17.3), and the
suites. Nothing here touches the user's data: every process ended is one this
script created, or one of the tree under it, by the PID it recorded.

No new dependency: `ctypes`, `urllib`, `subprocess`, Pillow for the screenshots
(the backend's), and the project's modules.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from ctypes import wintypes
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BACKEND = REPO / "backend"
FRONTEND = REPO / "frontend"
ELECTRON = FRONTEND / "node_modules" / "electron" / "dist" / "electron.exe"
RUNTIME = REPO / "runtime" / "postgres"
sys.path.insert(0, str(BACKEND))

from app.infrastructure.database.embedded.runtime import PostgresRuntime  # noqa: E402
from app.infrastructure.database.embedded.windows_processes import (  # noqa: E402
    WindowsProcesses,
)
from dataset_tools.manifest import (  # noqa: E402
    DEMO_CORPUS_ROOT,
    DEMO_MANIFEST_PATH,
    load_manifest,
)
from dataset_tools.materialize import materialize  # noqa: E402

WARM_RUNS = 5
OPEN_SECONDS = 300.0
JOB_SECONDS = 900.0
EXIT_SECONDS = 90.0
GONE_SECONDS = 30.0
SAMPLE_SECONDS = 0.1

WM_CLOSE = 0x0010
SW_MINIMIZE = 6
PW_CLIENT_ONLY_FULL_CONTENT = 0x1 | 0x2
CONSOLE_CLASSES = ("ConsoleWindowClass", "PseudoConsoleWindow")

CREATE_NO_WINDOW = 0x08000000
OCCLUSION_OFF = "--disable-features=CalculateNativeWinOcclusion"

NEVER_INDEXED = """
from sqlalchemy import text
from app.infrastructure.persistence.session import get_db
session = next(get_db())
session.execute(text(
    "INSERT INTO devices (id, volume_identity, volume_kind, label, first_seen_at, "
    "last_seen_at) VALUES (gen_random_uuid(), 'measure-hd4-never-indexed', "
    "'windows-volume-guid', 'HD4', now(), now()) ON CONFLICT DO NOTHING"
))
session.commit()
"""
"""A device that is registered, not plugged in and never indexed, written in the
work directory's database: `POST /devices` takes only a volume that is mounted,
and this machine has one."""

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
MARK = re.compile(
    r"^(?P<time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}) \| INFO \| shell \| "
    r"mark (?P<name>[\w-]+) \+(?P<ms>\d+) ms(?: (?P<detail>.*))?$"
)
EVENT = re.compile(r"\| shell \| event (?P<json>\{.*\})$")
HOME = re.compile(
    r'devices=(?P<devices>\d+) indexed=(?P<indexed>\d+) last=(?P<last>".*")'
)

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [ENUM_PROC, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.PostMessageW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.GetDIBits.argtypes = [
    wintypes.HDC,
    wintypes.HBITMAP,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.UINT,
]


# --------------------------------------------------------------------------
# Small things
# --------------------------------------------------------------------------


def say(text: str = "") -> None:
    print(f"    {text}" if text else "", flush=True)


def heading(title: str) -> None:
    print(f"\n## {title}\n", flush=True)


def seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f} s" if value < 10 else f"{value:.1f} s"


def spread(values: Sequence[float]) -> str:
    if not values:
        return "no samples"
    if len(values) == 1:
        return seconds(values[0])
    return (
        f"median {seconds(statistics.median(values))} "
        f"(min {seconds(min(values))}, max {seconds(max(values))}, n={len(values)})"
    )


def http(
    url: str,
    method: str = "GET",
    path: str = "/health",
    body: object = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> tuple[int, Any, dict[str, str]]:
    """One request: the status, the JSON or the text, the headers. 0 for none."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with OPENER.open(request, timeout=timeout) as answer:
            return int(answer.status), _payload(answer.read()), dict(answer.headers)
    except urllib.error.HTTPError as error:
        return int(error.code), _payload(error.read()), dict(error.headers)
    except OSError:
        return 0, None, {}


def _payload(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", errors="replace")


def wait_until(
    predicate: Callable[[], Any], seconds_: float, step: float = 0.1
) -> tuple[Any, float | None]:
    began = time.perf_counter()
    while time.perf_counter() - began < seconds_:
        answer = predicate()
        if answer:
            return answer, time.perf_counter() - began
        time.sleep(step)
    return None, None


def instant(text: str) -> datetime.datetime:
    """An ISO instant, to the millisecond -- what a JavaScript `Date` keeps."""
    moment = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


class CheckFailedError(Exception):
    """A criterion was not met. The message is the result."""


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------


def visible_windows() -> list[tuple[int, int, str, str]]:
    """Every visible top-level window: handle, PID, class and title."""
    found: list[tuple[int, int, str, str]] = []

    def visit(hwnd: int, _: int) -> bool:
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, name, 256)
            title = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, 256)
            found.append((int(hwnd), int(pid.value), name.value, title.value))
        return True

    user32.EnumWindows(ENUM_PROC(visit), 0)
    return found


def capture(hwnd: int, path: Path) -> str:
    """The client area of a window, occluded or not (`PrintWindow`)."""
    from PIL import Image

    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return f"no screenshot: the window is {width}x{height}"
    screen = user32.GetDC(hwnd)
    memory = gdi32.CreateCompatibleDC(screen)
    bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
    previous = gdi32.SelectObject(memory, bitmap)
    try:
        user32.PrintWindow(hwnd, memory, PW_CLIENT_ONLY_FULL_CONTENT)

        class BitmapInfoHeader(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        header = BitmapInfoHeader()
        header.biSize = ctypes.sizeof(BitmapInfoHeader)
        header.biWidth = width
        header.biHeight = -height  # top-down
        header.biPlanes = 1
        header.biBitCount = 32
        pixels = ctypes.create_string_buffer(width * height * 4)
        gdi32.GetDIBits(memory, bitmap, 0, height, pixels, ctypes.byref(header), 0)
        image = Image.frombuffer(
            "RGBA", (width, height), pixels.raw, "raw", "BGRA", 0, 1
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        image.convert("RGB").save(path)
    finally:
        gdi32.SelectObject(memory, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory)
        user32.ReleaseDC(hwnd, screen)
    return f"screenshot {path} ({width}x{height})"


# --------------------------------------------------------------------------
# A window
# --------------------------------------------------------------------------


class Opening:
    """One `electron.exe frontend`, its marks, its tree and its windows."""

    def __init__(self, m: Measurement, label: str, extra: dict[str, str]) -> None:
        self.m = m
        self.label = label
        self.offset = m.shell_log.stat().st_size if m.shell_log.exists() else 0
        self.stderr_path = m.out / f"{label}.electron.txt"
        self._stderr = self.stderr_path.open("wb")
        # A process that was there before the window cannot be of its tree: a
        # parent PID that Windows recycled would otherwise bring it in (measured:
        # the Java update scheduler, running since the logon).
        self.before = {row.pid for row in m.table.snapshot()}
        self.created_at = datetime.datetime.now()
        self.created = time.perf_counter()
        self.process = subprocess.Popen(
            [str(ELECTRON), OCCLUSION_OFF, str(FRONTEND)],
            cwd=FRONTEND,
            env=m.environment(extra),
            stdin=subprocess.DEVNULL,
            stdout=self._stderr,
            stderr=self._stderr,
        )
        self.tree: dict[int, str] = {self.process.pid: "electron.exe"}
        self.violations: list[str] = []
        self.most_processes = 0
        self._stop = threading.Event()
        self._sampler = threading.Thread(target=self._sample, daemon=True)
        self._sampler.start()

    # -- what it writes -----------------------------------------------------

    def lines(self) -> list[str]:
        if not self.m.shell_log.exists():
            return []
        with self.m.shell_log.open("rb") as file:
            file.seek(self.offset)
            text = file.read().decode("utf-8", errors="replace")
        return text.splitlines()

    def mark(self, name: str) -> tuple[datetime.datetime, str] | None:
        for line in self.lines():
            found = MARK.match(line)
            if found and found["name"] == name:
                moment = datetime.datetime.strptime(
                    found["time"], "%Y-%m-%d %H:%M:%S.%f"
                )
                return moment, found["detail"] or ""
        return None

    def since_created(self, name: str) -> float | None:
        found = self.mark(name)
        if found is None:
            return None
        return (found[0] - self.created_at).total_seconds()

    def events(self, name: str) -> list[dict[str, Any]]:
        found = []
        for line in self.lines():
            match = EVENT.search(line)
            if match:
                event = json.loads(match["json"])
                if event.get("event") == name:
                    found.append(event)
        return found

    def wait_mark(self, name: str, seconds_: float = OPEN_SECONDS) -> str:
        def seen() -> tuple[datetime.datetime, str] | None:
            if self.process.poll() is not None:
                raise CheckFailedError(
                    f"{self.label}: the window exited with {self.process.returncode} "
                    f"before the mark {name}; its log: {self.tail()}"
                )
            return self.mark(name)

        found, _ = wait_until(seen, seconds_)
        if found is None:
            raise CheckFailedError(f"{self.label}: no mark {name} in {seconds_:.0f} s")
        return str(found[1])

    def url(self) -> str:
        ready = self.events("ready") or self.events("already_running")
        if not ready or not ready[0].get("url"):
            raise CheckFailedError(f"{self.label}: no URL in shell.log")
        return str(ready[0]["url"])

    def tail(self, count: int = 8) -> str:
        return " / ".join(line for line in self.lines()[-count:])

    # -- its tree and its windows --------------------------------------------

    def _sample(self) -> None:
        while not self._stop.is_set():
            try:
                self._grow_tree()
                self._look_at_windows()
            except Exception as error:  # noqa: BLE001
                self.violations.append(f"the sampler failed: {error!r}")
                return
            self._stop.wait(SAMPLE_SECONDS)

    def _grow_tree(self) -> None:
        state = self.m.state()
        for pid in [*state.get("children", {}).values(), state.get("postmaster_pid")]:
            if (
                isinstance(pid, int)
                and pid not in self.tree
                and self.m.table.is_alive(pid)
            ):
                self.tree[pid] = "recorded"
        rows = self.m.table.snapshot()
        grown = True
        while grown:
            grown = False
            for row in rows:
                if (
                    row.parent_pid in self.tree
                    and row.pid not in self.tree
                    and row.pid not in self.before
                ):
                    self.tree[row.pid] = row.name
                    grown = True
        alive = sum(1 for pid in self.tree if self.m.table.is_alive(pid))
        self.most_processes = max(self.most_processes, alive)

    def _look_at_windows(self) -> None:
        for hwnd, pid, name, title in visible_windows():
            if pid == self.process.pid:
                continue  # the window itself
            ours = pid in self.tree
            console = name in CONSOLE_CLASSES and hwnd not in self.m.consoles_before
            if ours or console:
                found = (
                    f"a visible window {name!r} {title!r} of PID {pid} "
                    f"({self.tree.get(pid, 'not of the tree')})"
                )
                if found not in self.violations:
                    self.violations.append(found)

    def hwnd(self) -> int | None:
        for hwnd, pid, name, title in visible_windows():
            if pid == self.process.pid and "SolidVision" in title:
                return hwnd
        return None

    def screenshot(self, name: str) -> None:
        hwnd = self.hwnd()
        if hwnd is None:
            say(f"{self.label}: no window to capture for {name}")
            return
        say(capture(hwnd, self.m.shots / f"{name}.png"))

    # -- the close -------------------------------------------------------------

    def close(self) -> datetime.datetime:
        hwnd = self.hwnd()
        if hwnd is None:
            raise CheckFailedError(f"{self.label}: no visible window to close")
        moment = datetime.datetime.now()
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return moment

    def wait_exit(self, seconds_: float = EXIT_SECONDS) -> int:
        try:
            code = self.process.wait(seconds_)
        except subprocess.TimeoutExpired:
            raise CheckFailedError(
                f"{self.label}: the window is still open {seconds_:.0f} s after the "
                f"close; its log: {self.tail()}"
            ) from None
        return code

    def gone(self) -> tuple[list[str], float | None]:
        """The criterion of RFC-036 section 16, with the window's processes in it:
        every PID of the tree gone, and no process of the runtime this script did
        not find already running. How long after the window's exit it took."""
        self._grow_tree()

        def leftovers() -> list[str]:
            found = [
                f"{name} {pid}"
                for pid, name in sorted(self.tree.items())
                if self.m.table.is_alive(pid)
            ]
            found += [
                f"runtime process {pid}"
                for pid in self.m.runtime_processes()
                if pid not in self.m.runtime_before
            ]
            return found

        began = time.perf_counter()
        while time.perf_counter() - began < GONE_SECONDS:
            if not leftovers():
                return [], time.perf_counter() - began
            time.sleep(0.1)
        return leftovers(), None

    def finish(self) -> None:
        self._stop.set()
        self._sampler.join(5)
        if self.process.poll() is None:
            say(f"{self.label}: still open, closing it")
            try:
                self.close()
                self.wait_exit()
            except CheckFailedError:
                self.process.kill()
        self._stderr.close()


# --------------------------------------------------------------------------
# The measurement
# --------------------------------------------------------------------------


class Measurement:
    def __init__(self, work: Path, shots: Path, quiet_seconds: float) -> None:
        self.work = work
        self.local = work / "Local"
        self.data = self.local / "SolidVision-dev"
        self.shell_log = self.data / "logs" / "shell.log"
        self.api_log = self.data / "logs" / "api.log"
        self.out = work / "out"
        self.corpora = work / "corpora"
        self.shots = shots
        self.quiet_seconds = quiet_seconds
        for directory in (self.local, self.out):
            directory.mkdir(parents=True, exist_ok=True)
        self.table = WindowsProcesses()
        self.runtime = PostgresRuntime(RUNTIME)
        self.runtime_before = set(self.runtime_processes())
        self.consoles_before = {
            hwnd for hwnd, _, name, _ in visible_windows() if name in CONSOLE_CLASSES
        }
        self.openings: list[Opening] = []
        self.results: list[tuple[str, str]] = []

    def environment(self, extra: dict[str, str]) -> dict[str, str]:
        environment = {
            name: value
            for name, value in os.environ.items()
            if not name.upper().startswith(
                ("ELECTRON_", "SOLIDVISION_", "EMBEDDED_", "LOG_", "THUMBNAIL_")
            )
            and name.upper() not in ("API_PORT", "DATABASE_SOURCE", "WARM_UP_MODELS")
        }
        environment.update(
            LOCALAPPDATA=str(self.local), DATABASE_SOURCE="embedded", **extra
        )
        return environment

    def open(self, label: str, **extra: str) -> Opening:
        opening = Opening(self, label, extra)
        self.openings.append(opening)
        return opening

    def state(self) -> dict[str, Any]:
        try:
            return dict(json.loads((self.data / "launcher.json").read_text("utf-8")))
        except (OSError, ValueError):
            return {}

    def runtime_processes(self) -> list[int]:
        found = []
        for row in self.table.snapshot():
            executable = self.table.executable(row.pid)
            if executable is not None and self.runtime.contains(executable):
                found.append(row.pid)
        return found

    def corpus(self) -> Path:
        """The 45 images of RFC-022, of this work directory."""
        target = self.corpora / "demo"
        if not target.exists():
            materialize(load_manifest(DEMO_MANIFEST_PATH), DEMO_CORPUS_ROOT, target)
        return target

    def record(self, name: str, result: str) -> None:
        self.results.append((name, result))
        say(f"RESULT {name}: {result}")


def home_against_the_api(opening: Opening) -> str:
    """Section 10: the numbers on the screen are the ones GET /devices and GET
    /jobs give at the same moment."""
    detail = opening.wait_mark("home")
    url = opening.url()
    found = HOME.search(detail)
    if found is None:
        raise CheckFailedError(f"the home mark says nothing readable: {detail!r}")
    status, devices, _ = http(url, path="/api/v1/devices")
    status_jobs, jobs, _ = http(url, path="/api/v1/jobs?status=completed")
    if status != 200 or status_jobs != 200:
        raise CheckFailedError(f"GET /devices {status}, GET /jobs {status_jobs}")
    count = len(devices["devices"])
    indexed = sum(device["indexed_images"] for device in devices["devices"])
    finished = [
        instant(job["finished_at"]) for job in jobs["jobs"] if job["finished_at"]
    ]
    latest = max(finished) if finished else None
    shown = json.loads(found["last"])
    shown_latest = None if shown == "none" else instant(shown)
    screen = (int(found["devices"]), int(found["indexed"]), shown_latest)
    api = (count, indexed, latest)
    if screen != api:
        raise CheckFailedError(f"the screen showed {screen}, and the API says {api}")
    last = latest.isoformat() if latest else None
    return f"{count} devices, {indexed} photos, last {last}"


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------


def check_prepare(m: Measurement) -> None:
    """The first opening, cold: the cluster created, a device registered and the
    45 images of RFC-022 indexed through the API, so the home screen has numbers.
    """
    opening = m.open("prepare")
    opening.wait_mark("window", 60)
    say(f"window visible: {seconds(opening.since_created('window'))}")
    time.sleep(2.0)
    opening.screenshot("startup-first-run")
    opening.wait_mark("home")
    url = opening.url()
    phases = [event["phase"] for event in opening.events("phase")]
    say(f"phases: {', '.join(phases)}")
    say(f"ready at {url}: {seconds(opening.since_created('ready'))} after the process")
    folder = m.corpus()
    status, listed, _ = http(url, path="/api/v1/volumes")
    if status != 200:
        raise CheckFailedError(f"GET /api/v1/volumes: {status} {listed}")
    volumes = listed["volumes"]
    lowered = str(folder).lower()
    holding = max(
        (
            volume
            for volume in volumes
            if lowered.startswith(volume["mount_point"].lower().replace("/", "\\"))
        ),
        key=lambda volume: len(volume["mount_point"]),
    )
    status, device, _ = http(
        url,
        "POST",
        "/api/v1/devices",
        {
            "volume_identity": holding["volume_identity"],
            "volume_kind": holding["volume_kind"],
            "label": "Demo",
        },
    )
    if status >= 300:
        raise CheckFailedError(f"POST /api/v1/devices: {status} {device}")
    scope = folder.relative_to(Path(holding["mount_point"]))
    status, job, _ = http(
        url, "POST", "/api/v1/jobs", {"device_id": device["id"], "scopes": [str(scope)]}
    )
    if status >= 300:
        raise CheckFailedError(f"POST /api/v1/jobs: {status} {job}")
    say(f"queued {job['id']} over {scope}")

    def done() -> dict[str, Any] | None:
        _, now, _ = http(url, path=f"/api/v1/jobs/{job['id']}")
        if now and now.get("status") in ("completed", "failed", "cancelled"):
            return dict(now)
        return None

    finished, took = wait_until(done, JOB_SECONDS, 1.0)
    if finished is None or finished["status"] != "completed":
        raise CheckFailedError(f"the job did not complete: {finished}")
    say(
        f"indexed {finished['processed_images']} images in {seconds(took)} "
        f"({finished['failed_images']} failed)"
    )
    others = [
        volume
        for volume in volumes
        if volume is not holding and volume.get("device_id") is None
    ]
    if others:
        other = others[0]
        status, second, _ = http(
            url,
            "POST",
            "/api/v1/devices",
            {
                "volume_identity": other["volume_identity"],
                "volume_kind": other["volume_kind"],
                "label": "HD sem índice",
            },
        )
        say(f"registered {other['mount_point']} without indexing it: {status}")
    closed = opening.close()
    opening.wait_exit()
    stopped = opening.mark("stopped")
    say(
        "closed, stopped after "
        f"{seconds((stopped[0] - closed).total_seconds()) if stopped else 'n/a'}"
    )
    leftovers, _ = opening.gone()
    if leftovers or opening.violations:
        raise CheckFailedError(f"left: {leftovers}; windows: {opening.violations}")
    if not others:
        written = subprocess.run(
            [
                sys.executable,
                "-m",
                "app.infrastructure.database.embedded",
                "run",
                "--",
                sys.executable,
                "-c",
                NEVER_INDEXED,
            ],
            cwd=REPO,
            env=m.environment({"PYTHONPATH": str(BACKEND)}),
            capture_output=True,
            text=True,
            timeout=300,
            creationflags=CREATE_NO_WINDOW,
        )
        if written.returncode != 0:
            raise CheckFailedError(f"the device HD4 was not written: {written.stderr}")
        say(
            "no second volume: HD4, never indexed and not plugged in, written in "
            "the database"
        )
    m.record(
        "prepare", f"{finished['processed_images']} images indexed, ready at {url}"
    )


EXECUTOR_IN_ITS_LOOP = "| INFO | app.__main__ | Job executor started"
EXECUTOR_STOPPED_EARLY = "stopped by the operator before it started polling"


def openings(
    m: Measurement, label: str, settle: Callable[[Opening], str], shot: str | None
) -> tuple[dict[str, list[float]], list[str]]:
    """`WARM_RUNS` warm openings, each closed once `settle` has returned: the
    marks, the numbers against the API, the close, and the tree gone."""
    numbers_of: dict[str, list[float]] = {
        name: []
        for name in ("window", "ready", "home", "stopped", "exited", "gone", "executor")
    }
    problems: list[str] = []
    for run in range(1, WARM_RUNS + 1):
        opening = m.open(f"{label}-{run}")
        opening.wait_mark("window", 60)
        numbers = home_against_the_api(opening)
        if run == 1 and shot is not None:
            time.sleep(1.0)
            opening.screenshot(shot)
        values = [opening.since_created(name) for name in ("window", "ready", "home")]
        if None in values:
            raise CheckFailedError(f"run {run}: a mark is missing: {values}")
        for name, value in zip(("window", "ready", "home"), values, strict=True):
            numbers_of[name].append(value or 0.0)
        settled = settle(opening)
        closed = opening.close()
        opening.wait_exit()
        numbers_of["exited"].append((datetime.datetime.now() - closed).total_seconds())
        stopped = opening.mark("stopped")
        stop = "n/a"
        if stopped is None:
            problems.append(f"run {run}: no stopped mark; {opening.tail()}")
        else:
            numbers_of["stopped"].append((stopped[0] - closed).total_seconds())
            stop = seconds(numbers_of["stopped"][-1])
        executor = next(
            (line for line in opening.lines() if "job_runner stopped in" in line), ""
        )
        took_executor = re.search(r"stopped in ([\d.]+) s", executor)
        if took_executor:
            numbers_of["executor"].append(float(took_executor[1]))
        early = any(EXECUTOR_STOPPED_EARLY in line for line in opening.lines())
        leftovers, took = opening.gone()
        opening.finish()
        if leftovers:
            problems.append(f"run {run}: left {leftovers}")
        else:
            numbers_of["gone"].append(took or 0.0)
        problems += [f"run {run}: {found}" for found in opening.violations]
        say(
            f"run {run}: window {seconds(values[0])}, ready {seconds(values[1])}, "
            f"home {seconds(values[2])} ({numbers}); {settled}; close to stopped "
            f"{stop}, the job runner stopped in "
            f"{took_executor[1] + ' s' if took_executor else 'n/a'}"
            f"{' before it started polling' if early else ''}; to the window's exit "
            f"{seconds(numbers_of['exited'][-1])}; up to {opening.most_processes} "
            f"processes, gone {seconds(took)} after the exit"
        )
    say(f"process created to window visible: {spread(numbers_of['window'])}")
    say(f"process created to ready:          {spread(numbers_of['ready'])}")
    say(f"process created to home with data: {spread(numbers_of['home'])}")
    say(f"WM_CLOSE to stopped:               {spread(numbers_of['stopped'])}")
    say(f"  of which the job runner's stop:  {spread(numbers_of['executor'])}")
    say(f"WM_CLOSE to the window's exit:     {spread(numbers_of['exited'])}")
    say(f"the window's exit to all gone:     {spread(numbers_of['gone'])}")
    return numbers_of, problems


def check_warm(m: Measurement) -> None:
    """Five warm openings: the window, the home screen with the numbers the API
    gives, and the close with no job once the job executor is in its loop --
    the stop RFC-036 section 16 measured -- with nothing left behind."""

    def in_its_loop(opening: Opening) -> str:
        began = time.perf_counter()
        found, _ = wait_until(
            lambda: any(EXECUTOR_IN_ITS_LOOP in line for line in opening.lines()), 180
        )
        if not found:
            raise CheckFailedError(f"{opening.label}: the job executor never started")
        time.sleep(1.0)
        return f"the executor in its loop {seconds(time.perf_counter() - began)} later"

    numbers_of, problems = openings(m, "warm", in_its_loop, "home")
    if problems:
        raise CheckFailedError("; ".join(problems))
    m.record(
        "warm",
        f"window {seconds(statistics.median(numbers_of['window']))}, home "
        f"{seconds(statistics.median(numbers_of['home']))}, close to stopped "
        f"{seconds(statistics.median(numbers_of['stopped']))} (medians of "
        f"{WARM_RUNS}); nothing left behind, no console window",
    )


def check_early(m: Measurement) -> None:
    """Five warm openings closed a second after the home screen: the job executor
    is still loading the model, and the stop waits for it (section 16)."""

    def a_second(opening: Opening) -> str:
        time.sleep(1.0)
        loop = any(EXECUTOR_IN_ITS_LOOP in line for line in opening.lines())
        return (
            f"closed a second later, the executor {'in' if loop else 'not in'} its loop"
        )

    numbers_of, problems = openings(m, "early", a_second, None)
    if problems:
        raise CheckFailedError("; ".join(problems))
    m.record(
        "early",
        f"close to stopped {spread(numbers_of['stopped'])}; the job runner's own "
        f"stop {spread(numbers_of['executor'])}",
    )


def check_second(m: Measurement) -> None:
    """The second opening brings the first window to the front, and starts no
    second launcher."""
    first = m.open("second-first")
    first.wait_mark("home")
    hwnd = first.hwnd()
    if hwnd is None:
        raise CheckFailedError("the first window is not visible")
    user32.ShowWindow(hwnd, SW_MINIMIZE)
    time.sleep(1.0)
    if not user32.IsIconic(hwnd):
        raise CheckFailedError("the first window could not be minimized")
    pythons = sorted(pid for pid, name in first.tree.items() if name == "python.exe")
    began = time.perf_counter()
    second = subprocess.Popen(
        [str(ELECTRON), OCCLUSION_OFF, str(FRONTEND)],
        cwd=FRONTEND,
        env=m.environment({}),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        code = second.wait(60)
    except subprocess.TimeoutExpired:
        second.kill()
        raise CheckFailedError("the second window did not leave") from None
    took = time.perf_counter() - began
    restored, _ = wait_until(lambda: not user32.IsIconic(hwnd), 10)
    brought = any("A second opening brought" in line for line in first.lines())
    launchers = [line for line in first.lines() if "| shell | mark launcher " in line]
    first._grow_tree()
    pythons_after = sorted(
        pid for pid, name in first.tree.items() if name == "python.exe"
    )
    say(f"the second window exited with {code} in {seconds(took)}")
    say(f"the first restored: {bool(restored)}; its log says so: {brought}")
    say(f"launcher marks in the first's log: {len(launchers)}; pythons {pythons_after}")
    first.screenshot("second-opening")
    closed = first.close()
    first.wait_exit()
    stopped = first.mark("stopped")
    after = seconds((stopped[0] - closed).total_seconds()) if stopped else "n/a"
    say(f"closed; stopped {after} after the close")
    leftovers, _ = first.gone()
    if code != 0 or not restored or not brought or len(launchers) != 1:
        raise CheckFailedError(
            f"exit {code}, restored {bool(restored)}, logged {brought}, "
            f"launchers {len(launchers)}"
        )
    if pythons_after != pythons:
        raise CheckFailedError(f"the pythons changed: {pythons} -> {pythons_after}")
    if leftovers or first.violations:
        raise CheckFailedError(f"left: {leftovers}; windows: {first.violations}")
    m.record(
        "second", f"the second left with 0 in {seconds(took)}, the first came back"
    )


def check_reconnect(m: Measurement) -> None:
    """The API killed with the home screen open: Reconectando..., and the page
    again once the launcher has restarted it (the first risk of section 11)."""
    opening = m.open("reconnect")
    opening.wait_mark("home")
    api = m.state().get("children", {}).get("api")
    if not isinstance(api, int):
        raise CheckFailedError(f"no API in launcher.json: {m.state()}")

    def pages() -> int:
        return sum(1 for line in opening.lines() if "| shell | mark page " in line)

    def told(state: str) -> bool:
        return any(
            event.get("name") == "api" and event.get("state") == state
            for event in opening.events("child")
        )

    loaded = pages()
    began = time.perf_counter()
    m.table.terminate(api)
    restarting, _ = wait_until(lambda: told("restarting"), 30)
    time.sleep(0.3)
    opening.screenshot("reconnecting")
    running, _ = wait_until(lambda: told("running"), 120)
    reloaded, _ = wait_until(lambda: pages() > loaded, 60)
    took = time.perf_counter() - began
    say(
        f"api restarting told: {bool(restarting)}; running: {bool(running)}; the "
        f"page loaded again {seconds(took)} after the kill; address events: "
        f"{len(opening.events('address'))}"
    )
    time.sleep(1.5)
    opening.screenshot("reconnected")
    opening.close()
    opening.wait_exit()
    leftovers, _ = opening.gone()
    if not (restarting and running and reloaded):
        raise CheckFailedError(f"the page did not come back: {opening.tail()}")
    if leftovers or opening.violations:
        raise CheckFailedError(f"left: {leftovers}; windows: {opening.violations}")
    m.record("reconnect", f"the page back {seconds(took)} after the API was killed")


def check_guards(m: Measurement) -> None:
    """Host and Origin, against the real app: 400 and 403 (section 10)."""
    opening = m.open("guards")
    opening.wait_mark("home")
    url = opening.url()
    port = url.rsplit(":", 1)[1]
    rows: list[tuple[str, int, int]] = []

    def expect(what: str, expected: int, status: int) -> None:
        rows.append((what, expected, status))
        say(f"{what}: {status} (expected {expected})")

    evil, _, _ = http(url, headers={"Host": f"evil.example:{port}"})
    expect("GET /health, Host evil.example", 400, evil)
    local, _, _ = http(url, headers={"Host": f"localhost:{port}"})
    expect("GET /health, Host localhost", 200, local)
    job = "/api/v1/jobs/00000000-0000-0000-0000-000000000000/cancel"
    forged, _, _ = http(url, "POST", job, headers={"Origin": "http://evil.example"})
    expect("POST cancel, Origin evil.example", 403, forged)
    nulled, _, _ = http(url, "POST", job, headers={"Origin": "null"})
    expect("POST cancel, Origin null", 403, nulled)
    plain, _, _ = http(url, "POST", job)
    expect("POST cancel, no Origin (an unknown job)", 404, plain)
    own, _, _ = http(url, "POST", job, headers={"Origin": url})
    expect("POST cancel, the window's own Origin", 404, own)
    unknown, _, _ = http(url, "POST", "/api/v1/nada")
    expect("POST /api/v1/nada", 404, unknown)
    page, text, headers = http(url, path="/")
    expect("GET /, the index", 200, page)
    say(f"  Cache-Control: {headers.get('cache-control')}")
    screen, _, _ = http(url, path="/qualquer/tela")
    expect("GET /qualquer/tela, the index", 200, screen)
    script = re.search(
        r'src="(/assets/[^"]+\.js)"', text if isinstance(text, str) else ""
    )
    if script is None:
        raise CheckFailedError("the index names no script")
    asset, _, asset_headers = http(url, path=script[1])
    expect(f"GET {script[1]}", 200, asset)
    say(f"  ETag: {asset_headers.get('etag')}")
    missing, _, _ = http(url, path="/assets/falta.js")
    expect("GET /assets/falta.js", 404, missing)
    closed = opening.close()
    opening.wait_exit()
    del closed
    opening.gone()
    wrong = [
        f"{what}: {status}" for what, expected, status in rows if status != expected
    ]
    if wrong:
        raise CheckFailedError("; ".join(wrong))
    m.record("guards", f"{len(rows)} requests answered as expected")


def check_loading(m: Measurement) -> None:
    """With WARM_UP_MODELS=true, the home screen says the model is loading, and
    stops saying it once /health says ready."""
    opening = m.open("loading", WARM_UP_MODELS="true")
    opening.wait_mark("home")
    url = opening.url()
    _, health, _ = http(url)
    say(f"/health at the home mark: search {health.get('search') if health else None}")
    opening.screenshot("home-loading")
    began = time.perf_counter()
    answered, _ = wait_until(
        lambda: (http(url)[1] or {}).get("search")
        in ("ready", "unavailable", "on_demand"),
        120,
        0.5,
    )
    search = (http(url)[1] or {}).get("search")
    say(f"/health said {search} {seconds(time.perf_counter() - began)} later")
    time.sleep(3.0)
    opening.screenshot("home-search-ready")
    opening.close()
    opening.wait_exit()
    opening.gone()
    if not answered:
        raise CheckFailedError("search never left loading")
    m.record("loading", "the screenshots show the text, and its end")


def check_quiet(m: Measurement) -> None:
    """Ten minutes with the home screen open add no /health line to api.log or to
    shell.log (section 10)."""

    def count(path: Path) -> int:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return 0
        return sum(
            1
            for line in text.splitlines()
            if "/health" in line or "Health check" in line
        )

    opening = m.open("quiet", WARM_UP_MODELS="true")
    opening.wait_mark("home")
    before = (count(m.api_log), count(m.shell_log))
    say(f"health lines before: api.log {before[0]}, shell.log {before[1]}")
    # The page asks /health every 2 s while the model loads and every 30 s
    # after; this script asks too, every 15 s, so that the count does not rest
    # on the page alone.
    asked = 0
    began = time.perf_counter()
    while time.perf_counter() - began < m.quiet_seconds:
        status = http(opening.url())[0]
        asked += 1 if status == 200 else 0
        time.sleep(15.0)
    say(f"this script asked /health {asked} times, with 200")
    if opening.process.poll() is not None:
        raise CheckFailedError("the window closed during the wait")
    after = (count(m.api_log), count(m.shell_log))
    say(f"after {m.quiet_seconds:.0f} s: api.log {after[0]}, shell.log {after[1]}")
    opening.screenshot("after-the-wait")
    opening.close()
    opening.wait_exit()
    opening.gone()
    if after != before:
        raise CheckFailedError(f"health lines went from {before} to {after}")
    m.record("quiet", f"{m.quiet_seconds:.0f} s, no line added to either log")


CHECKS: dict[str, Callable[[Measurement], None]] = {
    "prepare": check_prepare,
    # Early before warm: the first openings after a cold start find the disk
    # still busy with what the indexing left (measured: ready in 12.9 to 19.2 s,
    # against 5.6 to 7.6 s a few openings later), and "warm" is RFC-036's: an
    # opening before it has put the files in the system's cache.
    "early": check_early,
    "warm": check_warm,
    "second": check_second,
    "reconnect": check_reconnect,
    "guards": check_guards,
    "loading": check_loading,
    "quiet": check_quiet,
}


def describe_machine() -> None:
    say(f"date: {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}")
    say(
        f"machine: {platform.platform()}, {platform.processor()}, {os.cpu_count()} "
        "logical CPUs"
    )
    say(f"python: {sys.version.split()[0]} ({sys.executable})")
    head = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    say(f"head: {head} (with the working tree of RFC-037)")
    version = subprocess.run(
        [str(ELECTRON), "--version"],
        capture_output=True,
        text=True,
        timeout=60,
        env={k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"},
    ).stdout.strip()
    say(f"electron: {version}")
    docker = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}} {{.Status}}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    say(
        f"docker: {docker.stdout.strip() or docker.stderr.strip() or 'nothing running'}"
    )
    say(f"elevated: {bool(ctypes.windll.shell32.IsUserAnAdmin())}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--only", default="")
    parser.add_argument("--work", type=Path)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--shots", type=Path)
    parser.add_argument("--quiet-seconds", type=float, default=600.0)
    arguments = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    # Physical pixels for the screenshots: Electron is per-monitor aware.
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    if arguments.list:
        for name, check in CHECKS.items():
            print(f"{name:10} {(check.__doc__ or '').splitlines()[0]}")
        return 0
    if (
        not (FRONTEND / "dist" / "index.html").is_file()
        or not (FRONTEND / "dist-electron" / "main.js").is_file()
    ):
        print("Run `npm run build` in frontend/ first.", file=sys.stderr)
        return 2
    chosen = [
        name
        for name in CHECKS
        if not arguments.only or name in arguments.only.split(",")
    ]
    work = arguments.work or Path(tempfile.mkdtemp(prefix="rfc037-measure-"))
    work.mkdir(parents=True, exist_ok=True)
    shots = arguments.shots or (work / "shots")
    print("# RFC-037: the window, measured\n")
    describe_machine()
    say(f"work directory: {work}")
    say(f"checks: {', '.join(chosen)}")
    measurement = Measurement(work, shots, arguments.quiet_seconds)
    failures = 0
    try:
        for name in chosen:
            heading(name)
            say((CHECKS[name].__doc__ or "").strip().splitlines()[0])
            began = time.perf_counter()
            try:
                CHECKS[name](measurement)
            except CheckFailedError as error:
                failures += 1
                measurement.record(name, f"FAILED: {error}")
            except Exception:
                failures += 1
                measurement.record(name, f"ERROR: {traceback.format_exc()}")
            finally:
                for opening in measurement.openings:
                    opening.finish()
                measurement.openings.clear()
            say(f"({name} took {seconds(time.perf_counter() - began)})")
        heading("results")
        for name, result in measurement.results:
            say(f"{name}: {result}")
    finally:
        if not arguments.keep:
            heading("cleanup")
            for _ in range(10):
                shutil.rmtree(work, ignore_errors=True)
                if not work.exists():
                    break
                time.sleep(1)
            say(f"work directory removed: {not work.exists()}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
