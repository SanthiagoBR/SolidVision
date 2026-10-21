r"""Devices and indexing, measured in the window (RFC-039 sections 8 and 13).

    python measure_devices.py > measure_devices.log 2>&1

    --list             the checks
    --only a,b         just these (in the order of --list); `prepare` always runs
    --work PATH        the work directory (default: a temporary one, on C:)
    --keep             keep it
    --disk X:          the external HD, for the checks a person has to do: plug
                       and unplug it when the script says so (`unplug`,
                       `midjob`), and the first criterion on it (`flow`).
                       Without it those run on C:, or say they did not run
    --small-disk Y:    a small disk -- a pen drive -- for `denominator`: a job of
                       the whole disk on it. Without it, the check says so
    --idle-minutes N   how long `idle` stays on the folders screen (default 10)

Every window is `frontend\node_modules\electron\dist\electron.exe frontend` over
the build -- `npm run build` first -- with `DATABASE_SOURCE=embedded`,
`WARM_UP_MODELS=true` and a `LOCALAPPDATA` of its own under the work directory,
as the measurements of RFC-037 and RFC-038 did: the data root, the cluster, the
logs and `shell.log` are the work directory's. The window is driven through
Chromium's debugging protocol (`--remote-debugging-port`): a label arrives by
`Input.insertText`, as from a keyboard, and the boxes and buttons by `click()`.
`--disable-features=CalculateNativeWinOcclusion` keeps a window behind the
editor painting (RFC-037 section 16).

The numbers of section 8 come from the `folders` marks of `shell.log` (section
5.9), written by the page: from the click to the response, and to the frame
after the list was drawn; and from a `PerformanceObserver` of `longtask` this
script puts in the page. The requests are counted by the protocol's `Network`
domain, which also gives the bodies the page sent.

**This opens windows on the desktop** -- the SolidVision window, several times
-- for about twenty minutes, ten of them on one screen that must be left alone.
With `--disk`, **it asks the person running it to plug and unplug the external
HD** when it says so, in the console. Leave the windows alone.

What it writes, and removes at the end: the RFC-022 dataset, materialized twice
and a tree of 5 000 folders, all under the work directory, on C:; with
`--disk`, the same two copies of the dataset in `X:\solidvision-rfc039\`. The C:
volume is registered as a device in the work directory's own database, never in
the user's.

No new dependency: a minimal WebSocket client over `socket`, `ctypes`,
`urllib`, `subprocess`, and the project's modules.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import datetime
import hashlib
import json
import os
import platform
import re
import select
import shutil
import socket
import statistics
import string
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from ctypes import wintypes
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BACKEND = REPO / "backend"
FRONTEND = REPO / "frontend"
ELECTRON = FRONTEND / "node_modules" / "electron" / "dist" / "electron.exe"
sys.path.insert(0, str(BACKEND))

from app.domain.value_objects.device_id import DeviceId  # noqa: E402
from app.domain.value_objects.image_path import ImagePath  # noqa: E402
from app.infrastructure.filesystem.image_identity import compute_image_id  # noqa: E402
from dataset_tools.manifest import (  # noqa: E402
    DEMO_CORPUS_ROOT,
    DEMO_MANIFEST_PATH,
    load_manifest,
)
from dataset_tools.materialize import materialize  # noqa: E402

RUNS = 5
OPEN_SECONDS = 300.0
JOB_SECONDS = 1800.0
MARK_SECONDS = 120.0
EXIT_SECONDS = 90.0
POLL_SECONDS = 0.05
DEBUG_PORT = 9339
OCCLUSION_OFF = "--disable-features=CalculateNativeWinOcclusion"
WM_CLOSE = 0x0010
CREATE_NO_WINDOW = 0x08000000
TREE_FOLDERS = 5_000
LABEL_C = "Sistema"
LABEL_DISK = "HD2"
LABEL_SMALL = "Pen"

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
"""A device registered, not plugged in and never indexed, written in the work
directory's database (RFC-037's measurement did the same): `POST /devices`
takes only a volume that is mounted, and this machine has one."""

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
MARK = re.compile(
    r"^(?P<time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}) \| INFO \| shell \| "
    r"mark (?P<name>[\w-]+) \+(?P<ms>\d+) ms(?: (?P<detail>.*))?$"
)
EVENT = re.compile(r"\| shell \| event (?P<json>\{.*\})$")
FOLDERS = re.compile(
    r"folders=(?P<folders>\d+) response=(?P<response>\d+) painted=(?P<painted>\d+)"
)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [ENUM_PROC, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.PostMessageW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
kernel32.GetLogicalDrives.restype = wintypes.DWORD


# --------------------------------------------------------------------------
# Small things
# --------------------------------------------------------------------------


def say(text: str = "") -> None:
    print(f"    {text}" if text else "", flush=True)


def ask(text: str) -> None:
    """A line for the person running the measurement, impossible to miss."""
    print(f"\n>>> {text}\n", flush=True)


def heading(title: str) -> None:
    print(f"\n## {title}\n", flush=True)


def ms(values: Sequence[float]) -> str:
    if not values:
        return "no samples"
    return (
        f"median {statistics.median(values):.0f} ms (min {min(values):.0f}, "
        f"max {max(values):.0f}, n={len(values)})"
    )


def http(
    url: str,
    path: str,
    method: str = "GET",
    body: object = None,
    timeout: float = 60.0,
) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with OPENER.open(request, timeout=timeout) as answer:
            return int(answer.status), _payload(answer.read())
    except urllib.error.HTTPError as error:
        return int(error.code), _payload(error.read())
    except OSError:
        return 0, None


def _payload(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", errors="replace")


def wait_until(
    predicate: Callable[[], Any], seconds: float, step: float = 0.1
) -> tuple[Any, float | None]:
    began = time.perf_counter()
    while time.perf_counter() - began < seconds:
        answer = predicate()
        if answer:
            return answer, time.perf_counter() - began
        time.sleep(step)
    return None, None


class CheckFailedError(Exception):
    """A criterion was not met. The message is the result."""


class SkippedError(Exception):
    """A check that cannot run here -- no disk -- and says why."""


def mounted_letters() -> set[str]:
    """The drive letters Windows lists right now: `GetLogicalDrives()`."""
    mask = int(kernel32.GetLogicalDrives())
    return {
        letter
        for index, letter in enumerate(string.ascii_uppercase)
        if mask >> index & 1
    }


def letter_of(drive: str | None) -> str | None:
    if not drive:
        return None
    letter = drive.strip().rstrip(":\\/").upper()
    if len(letter) != 1 or letter not in string.ascii_uppercase:
        raise SystemExit(f"Not a drive letter: {drive!r}")
    return letter


def visible_windows() -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []

    def visit(hwnd: int, _: int) -> bool:
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            found.append((int(hwnd), int(pid.value), title.value))
        return True

    user32.EnumWindows(ENUM_PROC(visit), 0)
    return found


# --------------------------------------------------------------------------
# Chromium's debugging protocol, over a WebSocket of the standard library
# --------------------------------------------------------------------------


class Cdp:
    """One page target: commands in, answers out; the events of the domains
    enabled are kept in `events`, as they arrive."""

    def __init__(self, ws_url: str) -> None:
        parts = urllib.parse.urlsplit(ws_url)
        host, port = parts.hostname or "127.0.0.1", parts.port or 80
        self._socket = socket.create_connection((host, port), timeout=120)
        key = base64.b64encode(os.urandom(16)).decode()
        self._socket.sendall(
            (
                f"GET {parts.path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        received = b""
        while b"\r\n\r\n" not in received:
            chunk = self._socket.recv(4096)
            if not chunk:
                raise ConnectionError("the debugging port closed the handshake")
            received += chunk
        head, self._buffer = received.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(head.decode(errors="replace"))
        accept = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()
        )
        if accept not in head:
            raise ConnectionError("the handshake was not accepted")
        self._next = 0
        self.events: list[dict[str, Any]] = []

    def _exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self._socket.recv(65536)
            if not chunk:
                raise ConnectionError("the debugging port closed")
            self._buffer += chunk
        taken, self._buffer = self._buffer[:count], self._buffer[count:]
        return taken

    def _send(self, payload: bytes, opcode: int = 1) -> None:
        length = len(payload)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", length)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._socket.sendall(header + mask + masked)

    def _message(self) -> str:
        data = b""
        while True:
            first, second = self._exact(2)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._exact(8))[0]
            payload = self._exact(length)
            opcode = first & 0x0F
            if opcode == 9:
                self._send(payload, 10)
                continue
            if opcode == 8:
                raise ConnectionError("the page target went away")
            data += payload
            if first & 0x80:
                return data.decode("utf-8")

    def _keep(self, message: dict[str, Any]) -> None:
        if "method" in message:
            self.events.append(message)

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        self._next += 1
        mine = self._next
        self._send(
            json.dumps({"id": mine, "method": method, "params": params}).encode()
        )
        while True:
            message = json.loads(self._message())
            if message.get("id") == mine:
                if "error" in message:
                    raise CheckFailedError(f"{method}: {message['error']}")
                return dict(message.get("result", {}))
            self._keep(message)

    def pump(self, seconds: float) -> None:
        """Read the events that arrive for `seconds`, with no command out."""
        end = time.perf_counter() + seconds
        while True:
            left = end - time.perf_counter()
            if left <= 0:
                return
            if not self._buffer:
                ready, _, _ = select.select([self._socket], [], [], min(left, 1.0))
                if not ready:
                    continue
            self._keep(json.loads(self._message()))

    def evaluate(self, expression: str) -> Any:
        result = self.call(
            "Runtime.evaluate",
            expression=expression,
            awaitPromise=True,
            returnByValue=True,
        )
        if "exceptionDetails" in result:
            raise CheckFailedError(f"the page threw: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    def click(self, selector: str, text: str | None = None) -> None:
        """Click the first enabled element of `selector` -- with this exact
        text, when one is given."""
        wanted = (
            f"e.textContent.trim() === {json.dumps(text)} && !e.disabled"
            if text is not None
            else "!e.disabled"
        )
        clicked = self.evaluate(
            "(() => { const e = [...document.querySelectorAll("
            f"{json.dumps(selector)})].find(e => {wanted});"
            " if (!e) return false; e.click(); return true; })()"
        )
        if not clicked:
            raise CheckFailedError(
                f"nothing enabled to click at {selector!r}"
                + (f" with {text!r}" if text is not None else "")
                + f"; the screen says {self.text()[:300]!r}"
            )

    def click_when_enabled(
        self, selector: str, text: str, seconds: float = 30.0
    ) -> None:
        """Click it once it is there and enabled: a button that waits for a
        reading of the API -- `/devices` again after a job ended."""
        ready, _ = wait_until(
            lambda: self.evaluate(
                f"[...document.querySelectorAll({json.dumps(selector)})].some(e =>"
                f" e.textContent.trim() === {json.dumps(text)} && !e.disabled)"
            ),
            seconds,
            0.1,
        )
        if not ready:
            raise CheckFailedError(
                f"{text!r} never enabled; the screen says {self.text()[:300]!r}"
            )
        self.click(selector, text)

    def type_into(self, selector: str, text: str) -> None:
        """Select a field and type over it: as from a keyboard."""
        found = self.evaluate(
            "(() => { const i = document.querySelector("
            f"{json.dumps(selector)}); if (!i) return false;"
            " i.focus(); i.select(); return true; })()"
        )
        if not found:
            raise CheckFailedError(f"no field at {selector!r}")
        self.call("Input.insertText", text=text)

    def text(self) -> str:
        return str(self.evaluate("document.body ? document.body.innerText : ''") or "")

    def path(self) -> str:
        return str(self.evaluate("location.pathname + location.search") or "")

    def requests(self, since: int = 0) -> list[dict[str, Any]]:
        """`Network.requestWillBeSent`'s requests, from event `since` on."""
        return [
            event["params"]["request"]
            for event in self.events[since:]
            if event.get("method") == "Network.requestWillBeSent"
        ]

    def close(self) -> None:
        try:
            self._socket.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# A window
# --------------------------------------------------------------------------


class Window:
    def __init__(self, m: Measurement, label: str, extra: dict[str, str]) -> None:
        self.m = m
        self.label = label
        self.offset = m.shell_log.stat().st_size if m.shell_log.exists() else 0
        self._stderr = (m.out / f"{label}.electron.txt").open("wb")
        self.process = subprocess.Popen(
            [
                str(ELECTRON),
                OCCLUSION_OFF,
                f"--remote-debugging-port={DEBUG_PORT}",
                str(FRONTEND),
            ],
            cwd=FRONTEND,
            env=m.environment(extra),
            stdin=subprocess.DEVNULL,
            stdout=self._stderr,
            stderr=self._stderr,
        )
        self.cdp: Cdp | None = None

    def lines(self, since: int | None = None) -> list[str]:
        if not self.m.shell_log.exists():
            return []
        with self.m.shell_log.open("rb") as file:
            file.seek(self.offset if since is None else since)
            return file.read().decode("utf-8", errors="replace").splitlines()

    def position(self) -> int:
        return self.m.shell_log.stat().st_size if self.m.shell_log.exists() else 0

    def marks(self, name: str, since: int | None = None) -> list[str]:
        found = []
        for line in self.lines(since):
            match = MARK.match(line)
            if match and match["name"] == name:
                found.append(match["detail"] or "")
        return found

    def wait_mark(
        self, name: str, since: int | None = None, seconds: float = OPEN_SECONDS
    ) -> str:
        def seen() -> str | None:
            if self.process.poll() is not None:
                raise CheckFailedError(
                    f"{self.label}: the window exited with {self.process.returncode}"
                )
            marks = self.marks(name, since)
            return marks[0] if marks else None

        found, _ = wait_until(seen, seconds)
        if found is None:
            raise CheckFailedError(f"{self.label}: no mark {name} in {seconds:.0f} s")
        return str(found)

    def url(self) -> str:
        for line in self.lines():
            match = EVENT.search(line)
            if match:
                event = json.loads(match["json"])
                if event.get("event") in ("ready", "already_running") and event.get(
                    "url"
                ):
                    return str(event["url"])
        raise CheckFailedError(f"{self.label}: no URL in shell.log")

    def connect(self) -> Cdp:
        url = self.url()

        def page() -> str | None:
            status, targets = http(f"http://127.0.0.1:{DEBUG_PORT}", "/json/list")
            if status != 200:
                return None
            for target in targets:
                if target.get("type") == "page" and str(
                    target.get("url", "")
                ).startswith(url):
                    return str(target["webSocketDebuggerUrl"])
            return None

        ws_url, _ = wait_until(page, 30)
        if ws_url is None:
            raise CheckFailedError(f"{self.label}: no page target at {url}")
        self.cdp = Cdp(ws_url)
        self.cdp.call("Network.enable")
        return self.cdp

    def folders_mark(self, since: int, folders: int) -> dict[str, int]:
        """The `folders` mark of a listing with this many folders, after
        `since`."""

        def seen() -> dict[str, int] | None:
            for detail in self.marks("folders", since):
                found = FOLDERS.search(detail)
                if found and int(found["folders"]) == folders:
                    return {key: int(value) for key, value in found.groupdict().items()}
            return None

        found, _ = wait_until(seen, MARK_SECONDS, POLL_SECONDS)
        if found is None:
            raise CheckFailedError(
                f"{self.label}: no folders mark with {folders} folders in "
                f"{MARK_SECONDS:.0f} s; the screen says "
                f"{self.cdp.text()[:300] if self.cdp else ''!r}"
            )
        return dict(found)

    def hwnd(self) -> int | None:
        for hwnd, pid, title in visible_windows():
            if pid == self.process.pid and "SolidVision" in title:
                return hwnd
        return None

    def close(self) -> None:
        if self.cdp is not None:
            self.cdp.close()
        if self.process.poll() is None:
            hwnd = self.hwnd()
            if hwnd is not None:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            try:
                self.process.wait(EXIT_SECONDS)
            except subprocess.TimeoutExpired:
                say(f"{self.label}: did not close in {EXIT_SECONDS:.0f} s; killed")
                self.process.kill()
        self._stderr.close()


# --------------------------------------------------------------------------
# The measurement
# --------------------------------------------------------------------------


class Measurement:
    def __init__(
        self,
        work: Path,
        disk: str | None,
        small_disk: str | None,
        idle_minutes: float,
    ) -> None:
        self.work = work.resolve()
        self.disk = disk
        self.small_disk = small_disk
        self.idle_minutes = idle_minutes
        self.local = self.work / "Local"
        self.data = self.local / "SolidVision-dev"
        self.logs = self.data / "logs"
        self.shell_log = self.logs / "shell.log"
        self.out = self.work / "out"
        self.corpora = self.work / "corpora"
        self.tree = self.work / "arvore"
        self.empty_models = self.work / "no-models"
        for directory in (self.local, self.out, self.empty_models):
            directory.mkdir(parents=True, exist_ok=True)
        self.window: Window | None = None
        self.url = ""
        self.c_device = ""
        self.results: list[tuple[str, str]] = []
        self.opened_paths: list[str] = []
        self.written_on_disk: list[Path] = []

    def environment(self, extra: dict[str, str]) -> dict[str, str]:
        environment = {
            name: value
            for name, value in os.environ.items()
            if not name.upper().startswith(
                (
                    "ELECTRON_",
                    "SOLIDVISION_",
                    "EMBEDDED_",
                    "LOG_",
                    "THUMBNAIL_",
                    "MODEL_",
                )
            )
            and name.upper() not in ("API_PORT", "DATABASE_SOURCE", "WARM_UP_MODELS")
        }
        environment.update(
            LOCALAPPDATA=str(self.local),
            DATABASE_SOURCE="embedded",
            WARM_UP_MODELS="true",
            **extra,
        )
        return environment

    def open(self, label: str, **extra: str) -> Window:
        if self.window is not None:
            self.window.close()
            self.window = None
        self.window = Window(self, label, extra)
        self.window.wait_mark("home")
        self.url = self.window.url()
        self.window.connect()
        return self.window

    @property
    def cdp(self) -> Cdp:
        if self.window is None or self.window.cdp is None:
            raise CheckFailedError("no window")
        return self.window.cdp

    def record(self, name: str, result: str) -> None:
        self.results.append((name, result))
        say(f"RESULT {name}: {result}")

    def goto(self, path: str) -> None:
        """The page at this path of the interface, loaded anew: an empty
        cache of TanStack Query, as a window that just opened has."""
        self.opened_paths.append(path)
        self.cdp.call("Page.navigate", url=self.url + path)
        wait_until(
            lambda: self.cdp.evaluate("document.readyState") == "complete", 30, 0.1
        )

    def devices(self) -> list[dict[str, Any]]:
        status, body = http(self.url, "/api/v1/devices")
        if status != 200:
            raise CheckFailedError(f"GET /devices: {status} {body}")
        return list(body["devices"])

    def device(self, device_id: str) -> dict[str, Any]:
        for device in self.devices():
            if device["id"] == device_id:
                return device
        raise CheckFailedError(f"no device {device_id}")

    def job(self, job_id: str) -> dict[str, Any]:
        status, body = http(self.url, f"/api/v1/jobs/{job_id}")
        if status != 200:
            raise CheckFailedError(f"GET /jobs/{job_id}: {status} {body}")
        return dict(body)

    def jobs_of(self, device_id: str) -> list[dict[str, Any]]:
        status, body = http(
            self.url, "/api/v1/jobs?" + urllib.parse.urlencode({"device_id": device_id})
        )
        if status != 200:
            raise CheckFailedError(f"GET /jobs: {status} {body}")
        return list(body["jobs"])

    def wait_job(
        self, job_id: str, states: Sequence[str], seconds: float = JOB_SECONDS
    ) -> dict[str, Any]:
        found, _ = wait_until(
            lambda: (job := self.job(job_id))["status"] in states and job, seconds, 0.5
        )
        if not found:
            raise CheckFailedError(f"job {job_id} never reached {states}")
        return dict(found)

    def corpus(self, root: Path) -> list[Path]:
        """RFC-022's 45 images, twice: two folders of 45 photos."""
        source = self.corpora / "demo"
        if not source.exists():
            materialize(load_manifest(DEMO_MANIFEST_PATH), DEMO_CORPUS_ROOT, source)
        copies = []
        for name in ("copia-a", "copia-b"):
            target = root / name
            if not target.exists():
                shutil.copytree(source, target)
            copies.append(target)
        return copies

    def register_through_the_dialog(self, letter: str, label: str) -> str:
        """`+ Adicionar dispositivo`, the row of this letter, the label, and
        `Adicionar`; the device's id, from the URL the dialog opens."""
        cdp = self.cdp
        cdp.click("button", "+ Adicionar dispositivo")
        row = (
            "[...document.querySelectorAll('[role=dialog] li')].find(li =>"
            f" li.querySelector('span') && li.querySelector('span').textContent.trim()"
            f" === {json.dumps(letter + ':')})"
        )
        found, _ = wait_until(lambda: cdp.evaluate(f"!!({row})"), 60, 0.2)
        if not found:
            raise CheckFailedError(
                f"the dialog does not list {letter}: {cdp.text()[:300]!r}"
            )
        known = cdp.evaluate(f"({row}).textContent")
        if "já é o" in str(known) or "já está registrado" in str(known):
            cdp.evaluate(
                f"[...({row}).querySelectorAll('button')]"
                ".find(b => b.textContent.trim() === 'Abrir').click()"
            )
        else:
            cdp.evaluate(
                f"({row}).querySelector('input').setAttribute('data-measure', '1')"
            )
            cdp.type_into("[data-measure='1']", label)
            cdp.evaluate(
                f"[...({row}).querySelectorAll('button')]"
                ".find(b => b.textContent.trim() === 'Adicionar').click()"
            )
        opened, _ = wait_until(
            lambda: (path := cdp.path()).startswith("/dispositivos/") and path, 60, 0.1
        )
        if not opened:
            raise CheckFailedError(
                f"the dialog did not open a device: {cdp.text()[:300]!r}"
            )
        return str(opened).split("/")[2].split("?")[0]


def relative_to_drive(path: Path) -> str:
    """`C:\\Users\\x\\corpora` gives `Users/x/corpora`: the `?pasta=` of the
    folders screen, which the API answers with the canonical spelling."""
    resolved = path.resolve()
    return "/".join(resolved.parts[1:])


def device_href(device_id: str, folder: str = "") -> str:
    base = f"/dispositivos/{urllib.parse.quote(device_id)}"
    if not folder:
        return base
    return (
        base
        + "?pasta="
        + "/".join(urllib.parse.quote(part, safe="") for part in folder.split("/"))
    )


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------


def check_prepare(m: Measurement) -> None:
    """The corpus on C:, the tree of 5 000 folders, HD4 never indexed, and a
    window at the home screen."""
    m.corpus(m.corpora)
    folder = m.tree / "pai"
    if not folder.exists():
        folder.mkdir(parents=True)
        for index in range(TREE_FOLDERS):
            child = folder / f"pasta {index + 1:04d}"
            child.mkdir()
            if index % 10 == 0:
                # A folder of its own, so that some rows have the arrow.
                (child / "dentro").mkdir()
    window = m.open("prepare")
    window.close()
    m.window = None
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
        raise CheckFailedError(f"HD4 was not written: {written.stderr[-400:]}")
    m.open("main")
    m.record(
        "prepare",
        f"2 x 45 photos and {TREE_FOLDERS} folders under {m.work}; HD4 written; "
        f"the window at {m.url}",
    )


def check_add(m: Measurement) -> None:
    """Section 5.4: C: registered through the dialog, with a label, and a body
    without a path."""
    cdp = m.cdp
    since = len(cdp.events)
    m.c_device = m.register_through_the_dialog("C", LABEL_C)
    found, _ = wait_until(lambda: LABEL_C in cdp.text(), 30, 0.2)
    if not found:
        raise CheckFailedError(f"the screen does not name {LABEL_C}")
    posts = [
        request
        for request in cdp.requests(since)
        if request["method"] == "POST" and request["url"].endswith("/api/v1/devices")
    ]
    if not posts:
        raise CheckFailedError("no POST /devices seen")
    body = json.loads(posts[-1].get("postData") or "{}")
    if set(body) != {"volume_identity", "volume_kind", "label"}:
        raise CheckFailedError(f"the body has {sorted(body)}")
    if re.search(r"[A-Za-z]:[\\/]", body["label"] + body["volume_kind"]):
        raise CheckFailedError(f"a path in the body: {body}")
    # Opened again, the dialog shows C: as a device already, with "Abrir".
    cdp.click("button", "+ Adicionar dispositivo")
    wait_until(lambda: "já é o" in cdp.text(), 30, 0.2)
    repeated = "já é o " + LABEL_C in cdp.text()
    cdp.call("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape")
    cdp.call("Input.dispatchKeyEvent", type="keyUp", key="Escape", code="Escape")
    m.record(
        "add",
        f"C: registered as {LABEL_C} ({m.c_device}) through the dialog; the body "
        f"carried {sorted(body)}; the dialog then said 'já é o {LABEL_C}': {repeated}",
    )


def _flow(m: Measurement, device_id: str, label: str, parent: Path) -> str:
    """The first criterion, on one device: two folders, index, cancel, index
    again; checked by the API at the end. Returns the result line."""
    cdp = m.cdp
    since = len(cdp.events)
    m.goto(device_href(device_id, relative_to_drive(parent)))
    wait_until(lambda: "copia-a" in cdp.text() and "copia-b" in cdp.text(), 60, 0.2)
    for name in ("copia-a", "copia-b"):
        cdp.click(f"li[data-path$='/{name}'] input[type=checkbox]")
    found, _ = wait_until(lambda: "2 pastas selecionadas" in cdp.text(), 10, 0.1)
    if not found:
        raise CheckFailedError(f"two folders not selected: {cdp.text()[:300]!r}")
    cdp.click("button", "Indexar as pastas")

    def first_job() -> dict[str, Any] | None:
        active = m.device(device_id)["active_job"]
        return dict(active) if active else None

    active, _ = wait_until(first_job, 30, 0.2)
    if not active:
        raise CheckFailedError("no job after Indexar as pastas")
    job_id = active["id"]
    started, _ = wait_until(
        lambda: (job := m.job(job_id))["processed_images"] >= 4 and job, 600, 0.5
    )
    if not started:
        raise CheckFailedError(f"the job never indexed 4 photos: {m.job(job_id)}")
    shown, _ = wait_until(lambda: "indexadas agora" in cdp.text(), 30, 0.2)
    cdp.click("button", "Cancelar")
    cancelling, _ = wait_until(
        lambda: "Cancelando" in cdp.text() or m.job(job_id)["status"] == "cancelled",
        30,
        0.05,
    )
    cancelled = m.wait_job(job_id, ("cancelled", "completed", "failed"), 120)
    if cancelled["status"] != "cancelled":
        raise CheckFailedError(f"the job ended {cancelled['status']}, not cancelled")
    said, _ = wait_until(lambda: "Cancelada" in cdp.text(), 30, 0.2)
    cdp.click_when_enabled("button", "Indexar de novo")
    second, _ = wait_until(
        lambda: (jobs := m.jobs_of(device_id)) and jobs[0]["id"] != job_id and jobs[0],
        30,
        0.2,
    )
    if not second:
        raise CheckFailedError("no second job after Indexar de novo")
    done = m.wait_job(second["id"], ("completed", "failed", "cancelled"))
    concluded, _ = wait_until(lambda: "Concluída" in cdp.text(), 60, 0.2)
    bodies = [
        json.loads(request.get("postData") or "{}")
        for request in cdp.requests(since)
        if request["method"] == "POST" and request["url"].endswith("/api/v1/jobs")
    ]
    checks = {
        "two scopes": sorted(cancelled["scopes"])
        == sorted(relative_to_drive(parent / n) for n in ("copia-a", "copia-b")),
        "a checkpoint": cancelled["last_processed_relative_path"] is not None,
        "completed": done["status"] == "completed",
        "skipped > 0": done["skipped_images"] > 0,
        "same scopes": sorted(done["scopes"]) == sorted(cancelled["scopes"]),
        "bodies of ids and listed paths": all(
            set(body) == {"device_id", "scopes"}
            and all(not re.match(r"^[A-Za-z]:", scope) for scope in body["scopes"])
            for body in bodies
        )
        and len(bodies) == 2,
        "panel: indexadas agora, Cancelando, Cancelada, Concluída": bool(
            shown and cancelling and said and concluded
        ),
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise CheckFailedError(f"{failed}: first {cancelled}; second {done}")
    return (
        f"{label}: 2 folders, cancelled at {cancelled['processed_images']} indexed "
        f"(checkpoint {cancelled['last_processed_relative_path']!r}); again: "
        f"{done['processed_images']} indexed, {done['skipped_images']} skipped, "
        f"{done['failed_images']} failed; POST bodies {bodies}"
    )


def check_flow(m: Measurement) -> None:
    """The first criterion: the external HD with --disk; rehearsed on C:
    without it."""
    if m.disk is None:
        line = _flow(m, m.c_device, LABEL_C, m.corpora)
        m.record("flow", f"rehearsed on C:, not an external HD -- {line}")
        return
    letter = m.disk
    if letter not in mounted_letters():
        ask(f"Conecte o HD externo em {letter}: agora.")
        found, _ = wait_until(lambda: letter in mounted_letters(), 600, 0.5)
        if not found:
            raise CheckFailedError(f"{letter}: never appeared")
    root = Path(f"{letter}:\\solidvision-rfc039")
    root.mkdir(exist_ok=True)
    m.written_on_disk.append(root)
    m.corpus(root)
    m.goto("/")
    device_id = m.register_through_the_dialog(letter, LABEL_DISK)
    m.record("flow", _flow(m, device_id, LABEL_DISK, root))


def _longtask_probe(cdp: Cdp) -> None:
    cdp.evaluate(
        "(() => { window.__longest = 0; new PerformanceObserver(list => {"
        " for (const e of list.getEntries()) window.__longest ="
        " Math.max(window.__longest, e.duration); }).observe({ type: 'longtask' });"
        " return true; })()"
    )


def check_folders(m: Measurement) -> None:
    """The third criterion: a folder of 5 000 folders, from the click on it to
    the list drawn, and the longest task of the page meanwhile."""
    window = m.window
    assert window is not None
    cdp = m.cdp
    marks: list[dict[str, int]] = []
    longest: list[float] = []
    rows: list[int] = []
    for run in range(RUNS):
        m.goto(device_href(m.c_device, relative_to_drive(m.tree)))
        found, _ = wait_until(
            lambda: cdp.evaluate(
                "!!document.querySelector(\"li[data-path$='/pai'] a\")"
            ),
            60,
            0.1,
        )
        if not found:
            raise CheckFailedError(f"no link to pai: {cdp.text()[:300]!r}")
        _longtask_probe(cdp)
        since = window.position()
        cdp.click("li[data-path$='/pai'] a")
        mark = window.folders_mark(since, TREE_FOLDERS)
        marks.append(mark)
        longest.append(float(cdp.evaluate("window.__longest") or 0))
        rows.append(
            int(cdp.evaluate("document.querySelectorAll('li[aria-posinset]').length"))
        )
        size = cdp.evaluate(
            "document.querySelector('li[aria-setsize]')?.getAttribute('aria-setsize')"
        )
        say(
            f"run {run + 1}: response {mark['response']} ms, painted "
            f"{mark['painted']} ms, longest task {longest[-1]:.0f} ms, {rows[-1]} rows "
            f"in the DOM, aria-setsize {size}"
        )
    # The filter, once: case and accents aside.
    cdp.type_into("input[type=search]", "PASTA 4999")
    filtered, _ = wait_until(
        lambda: cdp.evaluate("document.querySelectorAll('li[aria-posinset]').length")
        == 1,
        10,
        0.1,
    )
    m.record(
        "folders",
        f"response {ms([x['response'] for x in marks])}; painted "
        f"{ms([x['painted'] for x in marks])}; longest task {ms(longest)}; rows in the "
        f"DOM {sorted(set(rows))}; the filter left one row: {bool(filtered)}",
    )


def check_unplug(m: Measurement) -> None:
    """The second criterion: the HD pulled with its folders screen open, to the
    card saying "desconectado" and the screen "Conecte o HD2 para ver as
    pastas". Needs --disk and a person."""
    if m.disk is None:
        raise SkippedError("no --disk: the second criterion needs an external HD")
    letter = m.disk
    cdp = m.cdp
    device = next((d for d in m.devices() if d["label"] == LABEL_DISK), None)
    if letter not in mounted_letters():
        ask(f"Conecte o HD externo em {letter}: agora.")
        wait_until(lambda: letter in mounted_letters(), 600, 0.5)
    if device is None:
        m.goto("/")
        device_id = m.register_through_the_dialog(letter, LABEL_DISK)
    else:
        device_id = device["id"]
    connect_text = f"Conecte o {LABEL_DISK} para ver as pastas"
    times: list[float] = []
    for run in range(RUNS):
        m.goto(device_href(device_id))
        wait_until(lambda: "conectado em" in cdp.text(), 60, 0.2)
        ask(
            f"Rodada {run + 1} de {RUNS}: desconecte o HD de {letter}: agora "
            "(puxe o cabo, ou 'Ejetar' no Windows)."
        )
        gone, _ = wait_until(lambda: letter not in mounted_letters(), 600, POLL_SECONDS)
        if not gone:
            raise CheckFailedError(f"{letter}: never left")
        began = time.perf_counter()

        def both() -> bool:
            text = cdp.text()
            card = cdp.evaluate(
                "[...document.querySelectorAll('aside a')].some(a =>"
                f" a.textContent.includes({json.dumps(LABEL_DISK)}) &&"
                " a.textContent.includes('desconectado'))"
            )
            return bool(card) and connect_text in text

        shown, _ = wait_until(both, 30, POLL_SECONDS)
        if not shown:
            raise CheckFailedError(
                f"run {run + 1}: not shown in 30 s: {cdp.text()[:300]!r}"
            )
        times.append((time.perf_counter() - began) * 1000)
        say(f"run {run + 1}: {times[-1]:.0f} ms")
        if run < RUNS - 1:
            ask(f"Conecte o HD de novo em {letter}:.")
            wait_until(lambda: letter in mounted_letters(), 600, 0.5)
            back, _ = wait_until(lambda: "conectado em" in cdp.text(), 60, 0.2)
            say(f"back: {bool(back)}")
    messages = (
        sum(
            1
            for line in m.window.lines()
            if "Volumes changed (WM_DEVICECHANGE)" in line
        )
        if m.window
        else 0
    )
    m.record(
        "unplug",
        f"from the volume leaving to the card and the screen: {ms(times)}; "
        f"{messages} volume messages in shell.log",
    )


def check_midjob(m: Measurement) -> None:
    """The disk pulled in the middle of a job: the failure of section 5.7, and
    "Indexar de novo" waiting for the disk. Needs --disk and a person."""
    if m.disk is None:
        raise SkippedError("no --disk: needs an external HD and a person")
    letter = m.disk
    cdp = m.cdp
    device = next((d for d in m.devices() if d["label"] == LABEL_DISK), None)
    if device is None:
        raise SkippedError("the HD was not registered by `flow`")
    if letter not in mounted_letters():
        ask(f"Conecte o HD externo em {letter}: agora.")
        wait_until(lambda: letter in mounted_letters(), 600, 0.5)
    root = Path(f"{letter}:\\solidvision-rfc039")
    copy = root / "copia-c"
    if not copy.exists():
        shutil.copytree(m.corpora / "demo", copy)
    m.goto(device_href(device["id"], relative_to_drive(root)))
    wait_until(lambda: "copia-c" in cdp.text(), 60, 0.2)
    cdp.click("li[data-path$='/copia-c'] input[type=checkbox]")
    cdp.click("button", "Indexar as pastas")
    active, _ = wait_until(lambda: m.device(device["id"])["active_job"], 30, 0.2)
    if not active:
        raise CheckFailedError("no job")
    wait_until(lambda: m.job(active["id"])["processed_images"] >= 2, 600, 0.5)
    ask(f"Desconecte o HD de {letter}: agora, com a indexação em andamento.")
    wait_until(lambda: letter not in mounted_letters(), 600, POLL_SECONDS)
    failed = m.wait_job(active["id"], ("failed", "completed", "cancelled"), 300)
    text = f"A indexação parou porque o {LABEL_DISK} foi desconectado"
    shown, _ = wait_until(lambda: text in cdp.text(), 60, 0.2)
    disabled = cdp.evaluate(
        "[...document.querySelectorAll('button')].find(b =>"
        " b.textContent.trim() === 'Indexar de novo')?.disabled"
    )
    ask(f"Conecte o HD de novo em {letter}:.")
    wait_until(lambda: letter in mounted_letters(), 600, 0.5)
    enabled, _ = wait_until(
        lambda: cdp.evaluate(
            "[...document.querySelectorAll('button')].find(b =>"
            " b.textContent.trim() === 'Indexar de novo')?.disabled === false"
        ),
        60,
        0.2,
    )
    m.record(
        "midjob",
        f"the job ended {failed['status']} ({failed['error_message']!r}); the panel "
        f"said it: {bool(shown)}; Indexar de novo disabled with the disk away: "
        f"{disabled}, enabled with it back: {bool(enabled)}",
    )


def check_denominator(m: Measurement) -> None:
    """A job of the whole disk on a small disk: the percentage with today's date
    before the first photo, growing to 100%. Needs --small-disk."""
    if m.small_disk is None:
        raise SkippedError("no --small-disk: covered by the tests of section 7")
    letter = m.small_disk
    cdp = m.cdp
    if letter not in mounted_letters():
        ask(f"Conecte o disco pequeno em {letter}: agora.")
        wait_until(lambda: letter in mounted_letters(), 600, 0.5)
    m.goto("/")
    device_id = m.register_through_the_dialog(letter, LABEL_SMALL)
    cdp.click("button", "Indexar o disco inteiro")
    cdp.click("[role=alertdialog] button", "Indexar o disco inteiro")
    active, _ = wait_until(lambda: m.device(device_id)["active_job"], 30, 0.1)
    if not active:
        raise CheckFailedError("no job of the whole disk")

    def scanned() -> tuple[dict[str, Any], dict[str, Any]] | None:
        device = m.device(device_id)
        if device["last_scan_file_count"] is None:
            return None
        return device, m.job(active["id"])

    found, _ = wait_until(scanned, 600, 0.1)
    if not found:
        raise CheckFailedError("the denominator was never written")
    device, job = found
    done = m.wait_job(active["id"], ("completed", "failed", "cancelled"))
    full, _ = wait_until(lambda: "100% no scan de hoje" in cdp.text(), 60, 0.5)
    m.record(
        "denominator",
        f"{device['last_scan_file_count']} files counted at {device['last_scan_at']}, "
        f"with {job['processed_images']} photos indexed by the job then; the job "
        f"{done['status']} with {done['processed_images']} indexed; the card said "
        f"100% no scan de hoje: {bool(full)}",
    )


def check_banner(m: Measurement) -> None:
    """The job runner unavailable: the banner on the first screen, with its line,
    after a reload too; the buttons that index disabled."""
    window = m.open("no-models", MODEL_RUNTIME_DIRECTORY=str(m.empty_models))
    cdp = m.cdp
    banner = "A indexação está indisponível. A busca continua funcionando"
    shown, _ = wait_until(lambda: banner in cdp.text(), 120, 0.2)
    if not shown:
        raise CheckFailedError(f"no banner: {cdp.text()[:300]!r}")
    line = cdp.evaluate(
        "document.querySelector('[role=alert] p + p')?.textContent ?? ''"
    )
    cdp.call("Page.reload", ignoreCache=True)
    again, _ = wait_until(lambda: banner in cdp.text(), 60, 0.2)
    m.goto(device_href(m.c_device))
    disabled, _ = wait_until(
        lambda: cdp.evaluate(
            "[...document.querySelectorAll('button')].find(b =>"
            " b.textContent.trim() === 'Indexar o disco inteiro')?.disabled === true"
        )
        and "A indexação está indisponível." in cdp.text(),
        30,
        0.2,
    )
    events = [
        line_
        for line_ in window.lines()
        if '"name": "job_runner"' in line_ and '"unavailable"' in line_
    ]
    m.record(
        "banner",
        f"shown with the line {str(line)[:90]!r}; after a reload: {bool(again)}; "
        f"Indexar o disco inteiro disabled with the reason: {bool(disabled)}; "
        f"{len(events)} child event(s) of job_runner unavailable in shell.log",
    )
    m.open("main")


def check_paths(m: Measurement) -> None:
    """Section 5.11: the notice of the home screen, and the photo's detail."""
    cdp = m.cdp
    m.goto("/")
    found, _ = wait_until(lambda: "Escolher pastas" in cdp.text(), 30, 0.2)
    if not found:
        raise CheckFailedError(f"no Escolher pastas: {cdp.text()[:300]!r}")
    cdp.click("a", "Escolher pastas")
    hd4 = next((d for d in m.devices() if d["label"] == "HD4"), None)
    if hd4 is None:
        raise CheckFailedError("HD4 is not registered")
    opened, _ = wait_until(
        lambda: cdp.path() == device_href(hd4["id"])
        and "Conecte o HD4 para ver as pastas" in cdp.text(),
        30,
        0.2,
    )
    photo = _indexed_photo(m)
    relative = relative_to_drive(photo)
    image_id = str(
        compute_image_id(DeviceId(uuid.UUID(m.c_device)), ImagePath(relative)).value
    )
    # "Parecidas com esta" needs no model, and opens the detail over its grid.
    m.goto(f"/busca?parecidas={image_id}&foto={image_id}")
    link, _ = wait_until(lambda: f"Ver no {LABEL_C}" in cdp.text(), 60, 0.2)
    if not link:
        raise CheckFailedError(f"no Ver no {LABEL_C}: {cdp.text()[:300]!r}")
    cdp.click("a", f"Ver no {LABEL_C}")
    folder = "/".join(relative.split("/")[:-1])
    expected = f"/dispositivos/{m.c_device}?pasta={folder}"

    def there() -> str | None:
        here = cdp.evaluate(
            "document.querySelector('nav [aria-current=page]')?.textContent ?? null"
        )
        return here if urllib.parse.unquote(cdp.path()) == expected else None

    trail_end, _ = wait_until(there, 30, 0.2)
    if trail_end != folder.split("/")[-1]:
        raise CheckFailedError(f"at {cdp.path()!r}, the trail ending in {trail_end!r}")
    m.record(
        "paths",
        f"Escolher pastas opened HD4's screen, which said to plug it in: "
        f"{bool(opened)}; Ver no {LABEL_C} opened {folder!r}, the trail ending in "
        f"{trail_end!r}",
    )


def _indexed_photo(m: Measurement) -> Path:
    """A photo of C: that a completed job indexed."""
    for job in m.jobs_of(m.c_device):
        if job["status"] != "completed":
            continue
        for scope in job["scopes"]:
            photos = sorted((Path("C:/") / str(scope)).glob("**/*.jpg"))
            if photos:
                return Path(photos[0])
    raise CheckFailedError("no photo of C: indexed: run `flow` first")


def check_idle(m: Measurement) -> None:
    """Ten minutes on the folders screen, untouched: the requests by route."""
    cdp = m.cdp
    m.goto(device_href(m.c_device, relative_to_drive(m.corpora)))
    wait_until(lambda: "copia-a" in cdp.text(), 60, 0.2)
    cdp.pump(3)
    since = len(cdp.events)
    seconds = m.idle_minutes * 60
    say(f"untouched for {m.idle_minutes:g} minutes")
    cdp.pump(seconds)
    routes: Counter[str] = Counter()
    for request in cdp.requests(since):
        path = urllib.parse.urlsplit(request["url"]).path
        route = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27}", "{id}", path)
        routes[f"{request['method']} {route}"] += 1
    devices = routes.get("GET /api/v1/devices", 0)
    others = {
        route: count
        for route, count in routes.items()
        if "/volumes" in route or "/folders" in route
    }
    expected = seconds / 10
    if others or not expected * 0.8 <= devices <= expected * 1.2 + 1:
        raise CheckFailedError(f"{dict(routes)}")
    m.record(
        "idle",
        f"{m.idle_minutes:g} min: {dict(routes)}; /devices every "
        f"{seconds / max(devices, 1):.1f} s",
    )


def check_logs(m: Measurement) -> None:
    """No `?pasta=` value in the window's own lines of shell.log: the URLs leave
    without their query (section 5.1). The children's lines pass through the
    same file -- the job runner names every file it reads, in its own log too
    -- and are not the window's to redact."""
    log = m.shell_log.read_text(encoding="utf-8", errors="replace")
    lines = [line for line in log.splitlines() if "| shell |" in line]
    text = "\n".join(lines)
    leaks = re.findall(r"pasta=[^…\s\"]+", text)
    folders = [
        name for name in ("copia-a", "copia-b", "arvore", "pasta 4999") if name in text
    ]
    if leaks or folders:
        raise CheckFailedError(f"found {leaks[:5]} and {folders}")
    pages = sum(1 for line in lines if "mark page" in line)
    m.record(
        "logs",
        f"no ?pasta= value and no folder name in the window's {len(lines)} lines of "
        f"shell.log ({pages} pages loaded, {len(m.opened_paths)} paths asked)",
    )


CHECKS: dict[str, Callable[[Measurement], None]] = {
    "prepare": check_prepare,
    "add": check_add,
    "flow": check_flow,
    "folders": check_folders,
    "unplug": check_unplug,
    "midjob": check_midjob,
    "denominator": check_denominator,
    "paths": check_paths,
    "banner": check_banner,
    "idle": check_idle,
    "logs": check_logs,
}


def describe_machine() -> None:
    say(f"{platform.platform()}, {platform.processor()}")
    say(f"Python {platform.python_version()}, at {datetime.datetime.now().isoformat()}")
    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    say(f"checkout {head.stdout.strip()} plus the working tree")
    say(f"drives: {sorted(mounted_letters())}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--only", default="")
    parser.add_argument("--work", type=Path)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--disk")
    parser.add_argument("--small-disk")
    parser.add_argument("--idle-minutes", type=float, default=10.0)
    args = parser.parse_args()
    if args.list:
        print("\n".join(CHECKS))
        return 0
    if not (FRONTEND / "dist" / "index.html").is_file():
        print("Run `npm run build` in frontend/ first.")
        return 2
    chosen = [name for name in args.only.split(",") if name] or list(CHECKS)
    work = args.work or Path(tempfile.mkdtemp(prefix="rfc039-devices-"))
    heading("The machine")
    describe_machine()
    say(f"work directory {work}")
    m = Measurement(
        work, letter_of(args.disk), letter_of(args.small_disk), args.idle_minutes
    )
    failed = []
    try:
        for name, check in CHECKS.items():
            if name != "prepare" and name not in chosen:
                continue
            heading(name)
            try:
                check(m)
            except SkippedError as skipped:
                m.record(name, f"NOT RUN: {skipped}")
            except CheckFailedError as error:
                failed.append(name)
                m.record(name, f"FAILED: {error}")
                if name == "prepare":
                    break
            except Exception:  # noqa: BLE001
                failed.append(name)
                m.record(name, f"ERROR: {traceback.format_exc()}")
                if name == "prepare":
                    break
    finally:
        if m.window is not None:
            m.window.close()
        for written in m.written_on_disk:
            shutil.rmtree(written, ignore_errors=True)
        heading("Results")
        for name, result in m.results:
            say(f"{name}: {result}")
        if not args.keep and args.work is None:
            shutil.rmtree(work, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
