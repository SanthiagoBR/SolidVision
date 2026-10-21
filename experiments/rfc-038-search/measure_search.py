r"""The search, measured in the window (RFC-038 sections 8 and 13).

    python measure_search.py > measure_search.log 2>&1

    --list             the checks
    --only a,b         just these (in the order of --list); `prepare` always runs
    --work PATH        the work directory (default: a temporary one)
    --keep             keep it
    --dji PATH         the folder of real DJI photos, with GPS, for "perto daqui"
                       and the largest JPEG (default: %USERPROFILE%\Desktop\demo)

Every window is `frontend\node_modules\electron\dist\electron.exe frontend` over
the build -- `npm run build` first -- with `DATABASE_SOURCE=embedded`,
`WARM_UP_MODELS=true`, `ALLOW_LOCAL_FILE_ACTIONS=true` and a `LOCALAPPDATA` of
its own under the work directory, as RFC-037's measurement did: the data root,
the cluster, the logs and `shell.log` are the work directory's. The window is
driven through Chromium's debugging protocol (`--remote-debugging-port`): the
query arrives by `Input.insertText` and the Enter by `Input.dispatchKeyEvent`,
as from a keyboard, and a photo by `DOM.setFileInputFiles`, as from the file
picker. `--disable-features=CalculateNativeWinOcclusion` keeps a window behind
the editor painting (RFC-037 section 16).

The collection is RFC-022's 45 images copied three times, in three folders:
135 photos (section 2.9). The numbers come from the `search` marks of
`shell.log` (section 5.6), written by the page: from the gesture to the
response, and to the frame after the last thumbnail of the response decoded.

**This opens windows on the desktop** -- the SolidVision window, several times,
and one Explorer window -- for about fifteen minutes. Leave them alone.

The DJI folder is only read: its photos are indexed into the work directory's
own database and thumbnails, and the largest is sent as a query.

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
import shutil
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
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
sys.path.insert(0, str(BACKEND))

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
DEBUG_PORT = 9338
OCCLUSION_OFF = "--disable-features=CalculateNativeWinOcclusion"
WM_CLOSE = 0x0010
CREATE_NO_WINDOW = 0x08000000

# Five of RFC-022's 25 queries, in the Portuguese of RFC-035's measurement:
# the first of each pair, the same in every round (section 13).
QUERIES = (
    "propriedade rural com um pequeno lago",
    "propriedade em encosta com piscina",
    "tanques artificiais de piscicultura em um vale",
    "galpao industrial ao lado de terras agricolas",
    "terreno de obra limpo com terra vermelha exposta",
)
WARM_QUERY = "uma rua comercial com lojas e vitrines"
MODEL_LOADING_TEXT = "Carregando o modelo de busca."

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
MARK = re.compile(
    r"^(?P<time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}) \| INFO \| shell \| "
    r"mark (?P<name>[\w-]+) \+(?P<ms>\d+) ms(?: (?P<detail>.*))?$"
)
EVENT = re.compile(r"\| shell \| event (?P<json>\{.*\})$")
SEARCH = re.compile(
    r"kind=(?P<kind>\w+) limit=(?P<limit>\d+) results=(?P<results>\d+) "
    r"response=(?P<response>\d+) painted=(?P<painted>\d+)"
)

user32 = ctypes.WinDLL("user32", use_last_error=True)
ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [ENUM_PROC, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.PostMessageW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]


# --------------------------------------------------------------------------
# Small things
# --------------------------------------------------------------------------


def say(text: str = "") -> None:
    print(f"    {text}" if text else "", flush=True)


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


def visible_windows() -> list[tuple[int, int, str, str]]:
    found: list[tuple[int, int, str, str]] = []

    def visit(hwnd: int, _: int) -> bool:
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            name = ctypes.create_unicode_buffer(256)
            title = ctypes.create_unicode_buffer(512)
            user32.GetClassNameW(hwnd, name, 256)
            user32.GetWindowTextW(hwnd, title, 512)
            found.append((int(hwnd), int(pid.value), name.value, title.value))
        return True

    user32.EnumWindows(ENUM_PROC(visit), 0)
    return found


# --------------------------------------------------------------------------
# Chromium's debugging protocol, over a WebSocket of the standard library
# --------------------------------------------------------------------------


class Cdp:
    """One page target: commands in, answers out; events are dropped."""

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

    def type_and_enter(self, text: str) -> None:
        """Select the bar, type, Enter: as from a keyboard."""
        self.evaluate(
            "(() => { const i = document.querySelector('input[type=search]');"
            " i.focus(); i.select(); return true; })()"
        )
        self.call("Input.insertText", text=text)
        for kind in ("keyDown", "keyUp"):
            self.call(
                "Input.dispatchKeyEvent",
                type=kind,
                key="Enter",
                code="Enter",
                windowsVirtualKeyCode=13,
                nativeVirtualKeyCode=13,
                **({"text": "\r"} if kind == "keyDown" else {}),
            )

    def click_text(self, text: str, selector: str = "button") -> None:
        clicked = self.evaluate(
            "(() => { const b = [...document.querySelectorAll("
            f"{json.dumps(selector)})]"
            f".find(e => e.textContent.trim() === {json.dumps(text)});"
            " if (!b || b.disabled) return false; b.click(); return true; })()"
        )
        if not clicked:
            raise CheckFailedError(f"no enabled {selector} {text!r} on the screen")

    def choose_file(self, path: Path) -> None:
        root = self.call("DOM.getDocument", depth=0)["root"]["nodeId"]
        node = self.call("DOM.querySelector", nodeId=root, selector="input[type=file]")
        if not node.get("nodeId"):
            raise CheckFailedError("no file input on the screen")
        self.call("DOM.setFileInputFiles", files=[str(path)], nodeId=node["nodeId"])

    def grid_ids(self) -> list[str]:
        return list(
            self.evaluate(
                "[...document.querySelectorAll('li[data-id]')].map(e => e.dataset.id)"
            )
            or []
        )

    def text(self) -> str:
        return str(self.evaluate("document.body ? document.body.innerText : ''") or "")

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
        return self.cdp

    def search(
        self, gesture: Callable[[], None], kind: str, limit: int
    ) -> dict[str, int]:
        """Run a gesture and wait for the mark of the search it starts."""
        since = self.position()
        gesture()

        def seen() -> dict[str, int] | None:
            for detail in self.marks("search", since):
                found = SEARCH.search(detail)
                if found and found["kind"] == kind and int(found["limit"]) == limit:
                    return {
                        key: int(value)
                        for key, value in found.groupdict().items()
                        if key != "kind"
                    }
            return None

        found, _ = wait_until(seen, MARK_SECONDS, 0.05)
        if found is None:
            raise CheckFailedError(
                f"{self.label}: no search mark ({kind}, {limit}) in "
                f"{MARK_SECONDS:.0f} s; the screen says: "
                f"{self.cdp.text()[:300] if self.cdp else ''!r}"
            )
        return dict(found)

    def hwnd(self) -> int | None:
        for hwnd, pid, _, title in visible_windows():
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
    def __init__(self, work: Path, dji: Path) -> None:
        self.work = work
        self.dji = dji
        self.local = work / "Local"
        self.data = self.local / "SolidVision-dev"
        self.logs = self.data / "logs"
        self.shell_log = self.logs / "shell.log"
        self.query_images = self.data / "cache" / "query-images"
        self.out = work / "out"
        self.corpora = work / "corpora"
        self.empty_models = work / "no-models"
        for directory in (self.local, self.out, self.empty_models):
            directory.mkdir(parents=True, exist_ok=True)
        self.window: Window | None = None
        self.url = ""
        self.device_id = ""
        self.results: list[tuple[str, str]] = []
        self.typed: list[str] = list(QUERIES) + [WARM_QUERY]
        self.circles: list[str] = []

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
            and name.upper()
            not in (
                "API_PORT",
                "DATABASE_SOURCE",
                "WARM_UP_MODELS",
                "ALLOW_LOCAL_FILE_ACTIONS",
            )
        }
        environment.update(
            LOCALAPPDATA=str(self.local),
            DATABASE_SOURCE="embedded",
            WARM_UP_MODELS="true",
            ALLOW_LOCAL_FILE_ACTIONS="true",
            **extra,
        )
        return environment

    def open(self, label: str, **extra: str) -> Window:
        if self.window is not None:
            self.window.close()
        self.window = Window(self, label, extra)
        self.window.wait_mark("home")
        self.url = self.window.url()
        self.window.connect()
        return self.window

    def ready(self, seconds: float = 120) -> None:
        def answered() -> bool:
            _, body = http(self.url, "/health")
            return isinstance(body, dict) and body.get("search") == "ready"

        found, _ = wait_until(answered, seconds, 0.5)
        if not found:
            raise CheckFailedError(f"/health never said search: ready in {seconds} s")

    def record(self, name: str, result: str) -> None:
        self.results.append((name, result))
        say(f"RESULT {name}: {result}")

    def corpus(self) -> list[Path]:
        """RFC-022's 45 images, three times: 135 photos in three folders."""
        source = self.corpora / "demo"
        if not source.exists():
            materialize(load_manifest(DEMO_MANIFEST_PATH), DEMO_CORPUS_ROOT, source)
        copies = []
        for name in ("copia-a", "copia-b", "copia-c"):
            target = self.corpora / name
            if not target.exists():
                shutil.copytree(source, target)
            copies.append(target)
        return copies

    def index(self, folders: Sequence[Path], label: str) -> int:
        """Register the volume that holds the folders, once, and index them."""
        status, listed = http(self.url, "/api/v1/volumes")
        if status != 200:
            raise CheckFailedError(f"GET /volumes: {status} {listed}")
        lowered = str(folders[0]).lower()
        holding = max(
            (
                volume
                for volume in listed["volumes"]
                if lowered.startswith(volume["mount_point"].lower().replace("/", "\\"))
            ),
            key=lambda volume: len(volume["mount_point"]),
        )
        status, device = http(
            self.url,
            "/api/v1/devices",
            "POST",
            {
                "volume_identity": holding["volume_identity"],
                "volume_kind": holding["volume_kind"],
                "label": label,
            },
        )
        if status >= 300:
            raise CheckFailedError(f"POST /devices: {status} {device}")
        self.device_id = device["id"]
        mount = Path(holding["mount_point"])
        scopes = [str(folder.relative_to(mount)) for folder in folders]
        status, job = http(
            self.url,
            "/api/v1/jobs",
            "POST",
            {"device_id": device["id"], "scopes": scopes},
        )
        if status >= 300:
            raise CheckFailedError(f"POST /jobs: {status} {job}")

        def done() -> dict[str, Any] | None:
            _, now = http(self.url, f"/api/v1/jobs/{job['id']}")
            if isinstance(now, dict) and now.get("status") in (
                "completed",
                "failed",
                "cancelled",
            ):
                return now
            return None

        finished, took = wait_until(done, JOB_SECONDS, 1.0)
        if finished is None or finished["status"] != "completed":
            raise CheckFailedError(f"the job did not complete: {finished}")
        say(
            f"indexed {finished['processed_images']} images of {len(scopes)} folders "
            f"in {took:.1f} s ({finished['failed_images']} failed)"
        )
        return int(finished["processed_images"])

    def api_ids(self, query: str, limit: int) -> list[str]:
        path = "/api/v1/images/search?" + urllib.parse.urlencode(
            {"q": query, "limit": limit}
        )
        status, body = http(self.url, path)
        if status != 200:
            raise CheckFailedError(f"GET /search {status}: {body}")
        return [result["id"] for result in body["results"]]


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------


def check_prepare(m: Measurement) -> None:
    """The cluster created, the 135 photos indexed, and a warm window."""
    folders = m.corpus()
    window = m.open("prepare")
    _, listed = http(m.url, "/api/v1/devices")
    known = sum(device["indexed_images"] for device in listed["devices"])
    if known >= 135:
        # A work directory of an earlier run, passed again with --work.
        m.device_id = listed["devices"][0]["id"]
        say(f"{known} photos already indexed in {m.work}")
    else:
        count = m.index(folders, "Demo")
        if count != 135:
            raise CheckFailedError(f"{count} photos indexed, not 135")
    m.ready()
    window.close()
    m.window = None
    m.open("main")
    m.ready()
    m.record("prepare", f"135 photos indexed, the window at {m.url}")


def check_enter(m: Measurement) -> None:
    """Section 8: from the Enter to the grid painted with 40 thumbnails."""
    window = m.window
    assert window is not None and window.cdp is not None
    cdp = window.cdp
    window.search(lambda: cdp.type_and_enter(WARM_QUERY), "text", 40)
    rounds: list[tuple[list[float], list[float]]] = []
    for label in ("first time in the window", "again, thumbnails cached"):
        responses: list[float] = []
        painted: list[float] = []
        for query in QUERIES:
            m.ready()
            mark = window.search(lambda: cdp.type_and_enter(query), "text", 40)
            shown = cdp.grid_ids()
            if mark["results"] != 40 or len(shown) != 40:
                raise CheckFailedError(
                    f"{query!r}: {mark['results']} results, {len(shown)} cards"
                )
            expected = m.api_ids(query, 40)
            if shown != expected:
                raise CheckFailedError(f"{query!r}: the grid's order is not the API's")
            responses.append(mark["response"])
            painted.append(mark["painted"])
        say(f"{label}: response {ms(responses)}; painted {ms(painted)}")
        rounds.append((responses, painted))
    first = rounds[0]
    m.record(
        "enter",
        f"Enter to the grid of 40: {ms(first[1])}; the response {ms(first[0])}; "
        f"again with the thumbnails cached: {ms(rounds[1][1])}. The grid's ids were "
        "the API's, in its order, in all 10 searches",
    )


def _more(m: Measurement, clear_first: bool) -> list[float]:
    window = m.window
    assert window is not None and window.cdp is not None
    cdp = window.cdp
    painted: list[float] = []
    for query in QUERIES:
        m.ready()
        window.search(lambda: cdp.type_and_enter(query), "text", 40)
        if clear_first:
            cdp.call("Network.enable")
            cdp.call("Network.clearBrowserCache")
            cdp.call("Network.disable")
        mark = window.search(lambda: cdp.click_text("Mostrar mais"), "text", 100)
        shown = cdp.grid_ids()
        if mark["results"] != 100 or len(shown) != 100:
            raise CheckFailedError(f"{query!r}: {mark['results']} after Mostrar mais")
        if shown != m.api_ids(query, 100):
            raise CheckFailedError(f"{query!r}: the 100 are not in the API's order")
        if cdp.evaluate(
            "[...document.querySelectorAll('button')].some(b => "
            "b.textContent.trim() === 'Mostrar mais')"
        ):
            raise CheckFailedError("Mostrar mais is still drawn with 100 of 100")
        painted.append(mark["painted"])
    return painted


def check_more(m: Measurement) -> None:
    """Section 8: from the click on Mostrar mais to the 100th thumbnail."""
    _more(m, clear_first=False)
    warm = _more(m, clear_first=False)
    say(f"cache full (304s): {ms(warm)}")
    cold = _more(m, clear_first=True)
    say(f"cache empty: {ms(cold)}")
    m.record(
        "more",
        f"cache full {ms(warm)}; cache empty (informative) {ms(cold)}; the button "
        "gone with 100 of 100",
    )


def dataset_photo(m: Measurement) -> Path:
    from PIL import Image

    candidates = sorted((m.corpora / "demo").rglob("*.jpg"))
    for path in candidates:
        with Image.open(path) as picture:
            if picture.size == (1024, 768):
                return path
    return max(candidates, key=lambda path: path.stat().st_size)


def check_photo(m: Measurement) -> None:
    """Section 8: from the photo chosen to the grid painted with 40 thumbnails."""
    from PIL import Image

    window = m.window
    assert window is not None and window.cdp is not None
    cdp = window.cdp
    photo = dataset_photo(m)
    with Image.open(photo) as picture:
        size = f"{picture.size[0]} x {picture.size[1]}, {photo.stat().st_size} bytes"
    window.search(lambda: cdp.choose_file(photo), "image", 40)
    painted: list[float] = []
    responses: list[float] = []
    for _ in range(RUNS):
        m.ready()
        mark = window.search(lambda: cdp.choose_file(photo), "image", 40)
        if mark["results"] != 40 or len(cdp.grid_ids()) != 40:
            raise CheckFailedError(f"the photo gave {mark['results']} results")
        painted.append(mark["painted"])
        responses.append(mark["response"])
    say(f"{photo.name} ({size}): response {ms(responses)}; painted {ms(painted)}")
    leftovers = (
        sorted(p.name for p in m.query_images.iterdir())
        if m.query_images.is_dir()
        else []
    )
    if leftovers:
        raise CheckFailedError(f"cache\\query-images keeps {leftovers}")
    largest = max(
        (path for path in m.dji.glob("*.JPG")),
        key=lambda path: path.stat().st_size,
        default=None,
    )
    big = "no DJI folder"
    if largest is not None:
        with Image.open(largest) as picture:
            dims = f"{picture.size[0]} x {picture.size[1]}"
        big_painted: list[float] = []
        for _ in range(RUNS):
            m.ready()
            mark = window.search(lambda: cdp.choose_file(largest), "image", 40)
            big_painted.append(mark["painted"])
        big = (
            f"{largest.name}, {dims}, {largest.stat().st_size / 1e6:.1f} MB: "
            f"{ms(big_painted)}"
        )
        say(f"the largest JPEG at hand, {big}")
    m.record(
        "photo",
        f"{photo.name} ({size}): {ms(painted)}, the response {ms(responses)}; "
        f"{big}; cache\\query-images empty after",
    )


def check_upload(m: Measurement) -> None:
    """Section 8: the refusals by curl against the real app, and the folder after."""
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if curl is None:
        raise CheckFailedError("no curl on this machine")
    target = f"{m.url}/api/v1/images/search/by-image"
    text = m.work / "query.txt"
    text.write_text("not a picture", encoding="utf-8")
    large = m.work / "large.jpg"
    with large.open("wb") as file:
        file.write(b"\xff\xd8\xff")
        file.truncate(51 * 1024 * 1024)
    truncated = m.work / "truncated.jpg"
    whole = dataset_photo(m).read_bytes()
    truncated.write_bytes(whole[: len(whole) // 2])
    cases = (
        ("text/plain", text, 415),
        ("image/jpeg", large, 413),
        ("image/jpeg", truncated, 400),
    )
    answers = []
    for media_type, path, expected in cases:
        done = subprocess.run(
            [
                curl,
                "-s",
                "-o",
                os.devnull,
                "-w",
                "%{http_code}",
                "-H",
                f"Content-Type: {media_type}",
                "--data-binary",
                f"@{path}",
                target,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=CREATE_NO_WINDOW,
        )
        code = int(done.stdout.strip() or 0)
        answers.append(f"{path.name} as {media_type}: {code}")
        if code != expected:
            raise CheckFailedError(
                f"{path.name} as {media_type}: {code}, not {expected}"
            )
    leftovers = (
        sorted(p.name for p in m.query_images.iterdir())
        if m.query_images.is_dir()
        else []
    )
    if leftovers:
        raise CheckFailedError(f"cache\\query-images keeps {leftovers}")
    m.record("upload", "; ".join(answers) + "; cache\\query-images empty after")


def _open_detail(m: Measurement, image_id: str, query: str) -> Cdp:
    """The search, with the detail of `image_id` open over it: its URL, loaded."""
    window = m.window
    assert window is not None and window.cdp is not None
    cdp = window.cdp
    address = f"{m.url}/busca?" + urllib.parse.urlencode({"q": query, "foto": image_id})
    cdp.call("Page.navigate", url=address)
    found, _ = wait_until(
        lambda: "Mostrar no Explorer" in cdp.text() or "Copiar caminho" in cdp.text(),
        15,
    )
    if not found:
        raise CheckFailedError(f"the detail of {image_id} did not open")
    return cdp


def check_near(m: Measurement) -> None:
    """ "Buscar perto daqui" on a real DJI photo, through the window."""
    if not m.dji.is_dir():
        m.record("near", f"skipped: no {m.dji}")
        return
    m.index([m.dji], "Demo")
    placed: list[dict[str, Any]] = []
    for query in QUERIES:
        _, body = http(
            m.url,
            "/api/v1/images/search?"
            + urllib.parse.urlencode({"q": query, "limit": 100}),
        )
        placed = [r for r in body["results"] if r["latitude"] is not None]
        if placed:
            break
    if not placed:
        raise CheckFailedError("no DJI photo with a position in the results")
    photo = placed[0]
    window = m.window
    assert window is not None
    cdp = _open_detail(m, photo["id"], QUERIES[0])
    m.circles.append(f"{photo['latitude']}")
    m.circles.append(f"{photo['longitude']}")
    mark = window.search(lambda: cdp.click_text("Buscar perto daqui"), "text", 40)
    location = str(cdp.evaluate("window.location.search"))
    if "perto=" not in location:
        raise CheckFailedError(f"no circle in the URL: {location}")
    screen = cdp.text()
    if "Até 1 km de" not in screen:
        raise CheckFailedError("the circle's chip is not on the screen")
    _, near = http(
        m.url,
        "/api/v1/images/search?"
        + urllib.parse.urlencode(
            {
                "q": QUERIES[0],
                "limit": 40,
                "near_lat": photo["latitude"],
                "near_lon": photo["longitude"],
                "radius_m": 1000,
            }
        ),
    )
    if cdp.grid_ids() != [r["id"] for r in near["results"]]:
        raise CheckFailedError("the grid near the photo is not the API's")
    m.record(
        "near",
        f"{mark['results']} results within 1 km of {photo['relative_path']}, the "
        "grid the API's; excluded_unknown_position "
        f"{near['excluded_unknown_position']}",
    )


def check_explorer(m: Measurement) -> None:
    """The 204 opens the folder; a renamed file gives the 410 and its sentence."""
    _, body = http(
        m.url,
        "/api/v1/images/search?"
        + urllib.parse.urlencode({"q": QUERIES[0], "limit": 100}),
    )
    copies = [r for r in body["results"] if "copia-c/" in r["relative_path"]]
    if len(copies) < 2:
        raise CheckFailedError("fewer than two photos of copia-c in the results")
    before = {hwnd for hwnd, _, name, _ in visible_windows() if name == "CabinetWClass"}
    cdp = _open_detail(m, copies[0]["id"], QUERIES[0])
    cdp.click_text("Mostrar no Explorer")
    opened, took = wait_until(
        lambda: [
            (hwnd, title)
            for hwnd, _, name, title in visible_windows()
            if name == "CabinetWClass" and hwnd not in before
        ],
        20,
        0.2,
    )
    if not opened:
        raise CheckFailedError("no Explorer window opened")
    hwnd, title = opened[0]
    say(f"Explorer opened {title!r} in {took:.1f} s; closing it")
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    gone = copies[1]
    original = Path(gone["absolute_path"].replace("/", "\\"))
    renamed = original.with_name(original.stem + "-renomeado" + original.suffix)
    original.rename(renamed)
    try:
        cdp = _open_detail(m, gone["id"], QUERIES[0])
        cdp.click_text("Mostrar no Explorer")
        expected = f"O arquivo não está mais em {original.parent}."
        found, _ = wait_until(lambda: expected in cdp.text(), 10)
        if not found:
            raise CheckFailedError(f"no {expected!r} on the screen")
    finally:
        renamed.rename(original)
    m.record("explorer", f"204 opened {title!r}; the 410 said {expected!r}")


def check_nomodel(m: Measurement) -> None:
    """ "Parecidas com esta" with no model: the bar disabled, the results there."""
    _, body = http(
        m.url,
        "/api/v1/images/search?"
        + urllib.parse.urlencode({"q": QUERIES[1], "limit": 1}),
    )
    source = body["results"][0]["id"]
    window = m.open("no-model", MODEL_RUNTIME_DIRECTORY=str(m.empty_models))
    cdp = window.cdp
    assert cdp is not None

    def unavailable() -> dict[str, Any] | None:
        _, health = http(m.url, "/health")
        return (
            health
            if isinstance(health, dict) and health.get("search") == "unavailable"
            else None
        )

    health, _ = wait_until(unavailable, 120, 0.5)
    if not health:
        raise CheckFailedError("/health never said search: unavailable")
    detail = str(health.get("search_detail") or "")
    found, _ = wait_until(
        lambda: cdp.evaluate("document.querySelector('input[type=search]').disabled")
        and detail[:40] in cdp.text(),
        30,
    )
    if not found:
        raise CheckFailedError("the bar is not disabled with the search_detail")
    cdp.call("Page.navigate", url=f"{m.url}/busca?parecidas={source}")
    results, _ = wait_until(lambda: len(cdp.grid_ids()) > 0, 30, 0.2)
    if not results:
        raise CheckFailedError(f"Parecidas com esta gave no grid: {cdp.text()[:200]!r}")
    count = len(cdp.grid_ids())
    cdp.call(
        "Page.navigate",
        url=f"{m.url}/busca?" + urllib.parse.urlencode({"q": QUERIES[2]}),
    )
    message, _ = wait_until(
        lambda: cdp.evaluate(
            "(() => { const a = document.querySelector('[role=alert]');"
            " return a ? a.textContent : null; })()"
        ),
        60,
        0.5,
    )
    m.record(
        "nomodel",
        f"bar disabled with {detail[:60]!r}…; Parecidas com esta: {count} results; a "
        f"text search: {str(message)[:80]!r}",
    )


def check_warmup(m: Measurement) -> None:
    """The first search while the model warms up says why it waits."""
    window = m.open("warm-up")
    cdp = window.cdp
    assert cdp is not None
    _, health = http(m.url, "/health")
    state = health.get("search") if isinstance(health, dict) else None
    if state != "loading":
        m.record(
            "warmup", f"skipped: search was {state!r} when the home screen painted"
        )
        return
    since = window.position()
    began = time.perf_counter()
    cdp.type_and_enter(QUERIES[0])
    shown, _ = wait_until(lambda: MODEL_LOADING_TEXT in cdp.text(), 10, 0.1)
    _, health = http(m.url, "/health")
    while_loading = isinstance(health, dict) and health.get("search") == "loading"

    def mark() -> str | None:
        found = [d for d in window.marks("search", since) if "kind=text" in d]
        return found[0] if found else None

    detail, _ = wait_until(mark, MARK_SECONDS, 0.2)
    took = time.perf_counter() - began
    if not shown or detail is None:
        raise CheckFailedError(f"text shown: {bool(shown)}; mark: {detail}")
    if cdp.evaluate("document.querySelector('[role=alert]') !== null"):
        raise CheckFailedError("an error was shown after the warm-up")
    m.record(
        "warmup",
        f"the text shown (search still loading: {while_loading}), then the grid "
        f"{took:.1f} s after the Enter: {detail}",
    )


def check_logs(m: Measurement) -> None:
    """No query text and no circle's coordinates in any log of the data root."""
    needles = set()
    for query in m.typed:
        needles.update(
            {query, urllib.parse.quote(query), urllib.parse.quote_plus(query)}
        )
    needles.update(m.circles)
    hits = []
    files = sorted(path for path in m.logs.rglob("*") if path.is_file())
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in needles:
            if needle and needle in text:
                hits.append(f"{path.name}: {needle!r}")
    if hits:
        raise CheckFailedError(f"found: {hits[:10]}")
    m.record(
        "logs",
        f"0 occurrences of {len(m.typed)} queries ({len(needles) - len(m.circles)} "
        f"spellings) and {len(m.circles)} coordinates in "
        f"{', '.join(p.name for p in files)}",
    )


CHECKS: dict[str, Callable[[Measurement], None]] = {
    "prepare": check_prepare,
    "enter": check_enter,
    "more": check_more,
    "photo": check_photo,
    "upload": check_upload,
    "near": check_near,
    "explorer": check_explorer,
    "nomodel": check_nomodel,
    "warmup": check_warmup,
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


def main() -> int:
    # The screen's text has characters the console's code page does not.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--only", default="")
    parser.add_argument("--work", type=Path)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument(
        "--dji",
        type=Path,
        default=Path(os.environ.get("USERPROFILE", "~")) / "Desktop" / "demo",
    )
    args = parser.parse_args()
    if args.list:
        print("\n".join(CHECKS))
        return 0
    if not (FRONTEND / "dist" / "index.html").is_file():
        print("Run `npm run build` in frontend/ first.")
        return 2
    chosen = [name for name in args.only.split(",") if name] or list(CHECKS)
    work = args.work or Path(tempfile.mkdtemp(prefix="rfc038-search-"))
    heading("The machine")
    describe_machine()
    say(f"work directory {work}")
    m = Measurement(work, args.dji)
    failed = []
    try:
        for name, check in CHECKS.items():
            if name != "prepare" and name not in chosen:
                continue
            heading(name)
            try:
                check(m)
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
        heading("Results")
        for name, result in m.results:
            say(f"{name}: {result}")
        if not args.keep and args.work is None:
            shutil.rmtree(work, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
