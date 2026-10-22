r"""The map, measured in the window (RFC-040 sections 8 and 13).

    python measure_map.py > measure_map.log 2>&1

    --list             the checks
    --only a,b         just these (in the order of --list); `prepare` always runs
    --work PATH        the work directory (default: a temporary one, on C:)
    --keep             keep it
    --dji PATH         the folder of real DJI photos, with GPS (default:
                       %USERPROFILE%\Desktop\demo, RFC-032's 40)
    --offline MODE     `person` (default): a dialog asks the person running this
                       to turn the network off, and on again; `simulated`: the
                       window alone cannot resolve the tile host
                       (`--host-resolver-rules`), and the network stays on
    --drag-minutes N   how long `nobackground` drags and zooms (default 5)
    --skip-browser     `tiles` does not click the attribution's link: for a run
                       after one that opened the default browser already

Every window is `frontend\node_modules\electron\dist\electron.exe frontend` over
the build -- `npm run build` first -- with `DATABASE_SOURCE=embedded`,
`WARM_UP_MODELS=true` and a `LOCALAPPDATA` of its own under the work directory,
as the measurements of RFC-037 to 039 did: the data root, the cluster, the
logs and `shell.log` are the work directory's. The window is driven through
Chromium's debugging protocol (`--remote-debugging-port`) at its default size,
1280 x 800: the clicks, the drags and the wheel arrive by `Input.dispatch*`, as
from a mouse. `--disable-features=CalculateNativeWinOcclusion` keeps a window
behind the editor painting (RFC-037 section 16).

The numbers of section 8 come from the `map` marks of `shell.log` (section
5.6), written by the page: from the gesture to the response, and to the frame
after the cells were drawn. **The timed rounds run with "Fundo do
OpenStreetMap" off** (section 2.6): the tile usage policy forbids robots that
drag and zoom the map, and the numbers are the cells'. The tiles are checked
once, with the map opened and left alone.

**The database** has RFC-022's dataset and the 40 DJI photos, indexed through
the API before the rounds; then 100 000 rows with a position, written straight
into the work directory's cluster by this script (`--seed-rows`, run inside
`embedded run`), as `map_phase()` of RFC-032's `planner_check.py` did: one
device that is not connected, one vector for every row -- the map reads only
`embedding IS NOT NULL` --, 90% with a position around 2 000 places drawn
inside the outline of the states, with Zipf weights (s = 0.85) and 60 m of
scatter, and dates drawn from 2015 to 2026, 5% with none. The HNSW index is
dropped first, as `map_phase()` dropped it: 100 000 identical vectors would
make its upkeep the cost of the seeding, and on the embedded PostgreSQL 16 it
never serves a search (RFC-038 section 14.1).

**This opens windows on the desktop** -- the SolidVision window, several times
-- for about forty minutes, five of them dragging and zooming the map; **it
opens the default browser once**, by the attribution's link; and, with
`--offline person`, **it asks the person running it to turn the network off and
on again**, in a dialog on the screen. Leave the windows alone.

What it writes, and removes at the end: RFC-022's dataset, materialized under
the work directory, on C:. The DJI folder is only read. Everything lands in
the work directory's own database, never the user's.

No new dependency: a minimal WebSocket client over `socket`, `ctypes`,
`urllib`, `subprocess`, Pillow for the screenshots, and the project's modules.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import datetime
import hashlib
import io
import json
import math
import os
import platform
import random
import re
import select
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
import uuid
from collections.abc import Callable, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BACKEND = REPO / "backend"
FRONTEND = REPO / "frontend"
ELECTRON = FRONTEND / "node_modules" / "electron" / "dist" / "electron.exe"
OUTLINE = FRONTEND / "src" / "map" / "brazil-states.json"
sys.path.insert(0, str(BACKEND))

RUNS = 5
OPEN_SECONDS = 300.0
JOB_SECONDS = 1800.0
MARK_SECONDS = 180.0
EXIT_SECONDS = 90.0
NETWORK_SECONDS = 300.0
POLL_SECONDS = 0.05
DEBUG_PORT = 9340
OCCLUSION_OFF = "--disable-features=CalculateNativeWinOcclusion"
SIMULATED_OFFLINE = "--host-resolver-rules=MAP tile.openstreetmap.org ~NOTFOUND"
WM_CLOSE = 0x0010
CREATE_NO_WINDOW = 0x08000000
TILE_HOST = "tile.openstreetmap.org"
OSM_STORAGE_KEY = "solidvision.mapa.fundo-osm"
OFFLINE_TEXT = "Sem conexão com o OpenStreetMap"
WINDOW_UA = "SolidVision/0.1.0 (+https://github.com/SanthiagoBR/SolidVision)"

SEED_ROWS = 100_000
SEED_SITES = 2_000
ZIPF_S = 0.85
SCATTER_M = 60.0
METRES_PER_DEGREE = 111_195.0
POSITION_FRACTION = 0.90
UNDATED_FRACTION = 0.05
BENCH_DEVICE = uuid.UUID(int=0xD0000040)
EARTH_RADIUS_M = 6_371_008.8

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
MARK = re.compile(
    r"^(?P<time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}) \| INFO \| shell \| "
    r"mark (?P<name>[\w-]+) \+(?P<ms>\d+) ms(?: (?P<detail>.*))?$"
)
EVENT = re.compile(r"\| shell \| event (?P<json>\{.*\})$")
MAP_MARK = re.compile(
    r"cells=(?P<cells>\d+) precision=(?P<requested>\d+)->(?P<applied>\d+) "
    r"response=(?P<response>\d+) painted=(?P<painted>\d+)"
)
SEARCH_MARK = re.compile(
    r"kind=(?P<kind>\w+) limit=(?P<limit>\d+) results=(?P<results>\d+) "
    r"response=(?P<response>\d+) painted=(?P<painted>\d+)"
)
TILE_PATH = re.compile(r"tile\.openstreetmap\.org/\d")

user32 = ctypes.WinDLL("user32", use_last_error=True)
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
user32.MessageBoxW.argtypes = [
    wintypes.HWND,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.UINT,
]
MB_OK_TOPMOST = 0x0 | 0x40 | 0x40000 | 0x10000  # OK, information, topmost, foreground


# --------------------------------------------------------------------------
# Small things
# --------------------------------------------------------------------------


def say(text: str = "") -> None:
    print(f"    {text}" if text else "", flush=True)


def heading(title: str) -> None:
    print(f"\n## {title}\n", flush=True)


def ask(text: str) -> None:
    """A dialog on the screen for the person running this, and the same line
    in the log: the console is not where they look."""
    print(f"\n>>> {text}\n", flush=True)
    user32.MessageBoxW(None, text, "Medição do RFC-040", MB_OK_TOPMOST)


def ms(values: Sequence[float]) -> str:
    if not values:
        return "no samples"
    return (
        f"median {statistics.median(values):.0f} ms (min {min(values):.0f}, "
        f"max {max(values):.0f}, n={len(values)})"
    )


def http(
    url: str, path: str, method: str = "GET", body: object = None
) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with OPENER.open(request, timeout=120) as answer:
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


def resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443)
    except OSError:
        return False
    return True


def flush_dns() -> None:
    subprocess.run(
        ["ipconfig", "/flushdns"],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """The backend's formula and radius (`haversine.py`)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def project(lat: float, lon: float, zoom: float) -> tuple[float, float]:
    """The Web Mercator pixel of a point at a zoom, as Leaflet's EPSG:3857."""
    scale = 256 * 2**zoom
    lat = max(-85.0511287798, min(85.0511287798, lat))
    sine = math.sin(math.radians(lat))
    x = (lon + 180) / 360 * scale
    y = (0.5 - math.log((1 + sine) / (1 - sine)) / (4 * math.pi)) * scale
    return x, y


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
# The outline: the 27 units, a point inside each, and the places of the seed
# --------------------------------------------------------------------------


def _rings(geometry: dict[str, Any]) -> list[list[list[list[float]]]]:
    """Polygons as lists of rings, for Polygon and MultiPolygon alike."""
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"]]
    return list(geometry["coordinates"])


def _inside_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
    inside = False
    for index in range(len(ring)):
        x1, y1 = ring[index - 1]
        x2, y2 = ring[index]
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def inside_polygons(
    lon: float, lat: float, polygons: list[list[list[list[float]]]]
) -> bool:
    for polygon in polygons:
        outer, *holes = polygon
        if _inside_ring(lon, lat, outer) and not any(
            _inside_ring(lon, lat, hole) for hole in holes
        ):
            return True
    return False


def bounded(
    polygons: list[list[list[list[float]]]],
) -> list[tuple[tuple[float, float, float, float], list[list[list[float]]]]]:
    """Each polygon with its box, so that a point far from it costs one test."""
    boxed = []
    for polygon in polygons:
        lons = [vertex[0] for vertex in polygon[0]]
        lats = [vertex[1] for vertex in polygon[0]]
        boxed.append(((min(lons), min(lats), max(lons), max(lats)), polygon))
    return boxed


def inside_boxed(
    lon: float,
    lat: float,
    boxed: list[tuple[tuple[float, float, float, float], list[list[list[float]]]]],
) -> bool:
    return any(
        box[0] <= lon <= box[2]
        and box[1] <= lat <= box[3]
        and inside_polygons(lon, lat, [polygon])
        for box, polygon in boxed
    )


@dataclass(frozen=True)
class Unit:
    sigla: str
    polygons: list[list[list[list[float]]]]
    inner_lat: float
    inner_lon: float


def load_units() -> list[Unit]:
    """Each unit, with a point well inside it: of a grid over its box, the one
    inside the polygon farthest from every vertex."""
    outline = json.loads(OUTLINE.read_text(encoding="utf-8"))
    units = []
    for feature in outline["features"]:
        polygons = _rings(feature["geometry"])
        vertices = [
            vertex for polygon in polygons for ring in polygon for vertex in ring
        ]
        lons = [vertex[0] for vertex in vertices]
        lats = [vertex[1] for vertex in vertices]
        best: tuple[float, float, float] | None = None
        steps = 40
        for i in range(1, steps):
            for j in range(1, steps):
                lon = min(lons) + (max(lons) - min(lons)) * i / steps
                lat = min(lats) + (max(lats) - min(lats)) * j / steps
                if not inside_polygons(lon, lat, polygons):
                    continue
                clearance = min(
                    (vertex[0] - lon) ** 2 + (vertex[1] - lat) ** 2
                    for vertex in vertices
                )
                if best is None or clearance > best[0]:
                    best = (clearance, lat, lon)
        if best is None:
            raise CheckFailedError(f"no point inside {feature['properties']['sigla']}")
        units.append(Unit(feature["properties"]["sigla"], polygons, best[1], best[2]))
    return units


# --------------------------------------------------------------------------
# The 100 000 rows, written inside `embedded run` (`--seed-rows N`)
# --------------------------------------------------------------------------


def seed_plan(rows: int) -> tuple[list[tuple[float, float]], list[dict[str, Any]]]:
    """The places and the rows, from a fixed seed: 2 000 places inside the
    outline with Zipf weights, 90% of the rows around one of them with 60 m of
    scatter, and dates from 2015 to 2026 with 5% unknown."""
    generator = random.Random(40)
    outline = json.loads(OUTLINE.read_text(encoding="utf-8"))
    polygons = [
        p for feature in outline["features"] for p in _rings(feature["geometry"])
    ]
    boxed = bounded(polygons)
    lons = [v[0] for p in polygons for ring in p for v in ring]
    lats = [v[1] for p in polygons for ring in p for v in ring]
    places: list[tuple[float, float]] = []
    while len(places) < SEED_SITES:
        lon = generator.uniform(min(lons), max(lons))
        lat = generator.uniform(min(lats), max(lats))
        if inside_boxed(lon, lat, boxed):
            places.append((lat, lon))
    cumulative: list[float] = []
    total = 0.0
    for rank in range(1, SEED_SITES + 1):
        total += 1 / (rank**ZIPF_S)
        cumulative.append(total)
    start = datetime.datetime(2015, 1, 1)
    span = (datetime.datetime(2027, 1, 1) - start).total_seconds()
    scatter = SCATTER_M / METRES_PER_DEGREE
    planned = []
    for _ in range(rows):
        position: tuple[float, float] | None = None
        if generator.random() < POSITION_FRACTION:
            lat, lon = generator.choices(places, cum_weights=cumulative)[0]
            position = (
                lat + generator.gauss(0, scatter),
                lon + generator.gauss(0, scatter) / math.cos(math.radians(lat)),
            )
        captured = (
            None
            if generator.random() < UNDATED_FRACTION
            else start + datetime.timedelta(seconds=generator.uniform(0, span))
        )
        planned.append({"position": position, "captured": captured})
    return places, planned


def seed_rows(rows: int) -> int:
    """Run inside `embedded run`: the device, the rows, ANALYZE."""
    from sqlalchemy import text

    from app.infrastructure.persistence.session import get_db

    _, planned = seed_plan(rows)
    vector = "[" + ",".join(f"{value:.6f}" for value in unit_vector(7)) + "]"
    session = next(get_db())
    began = time.perf_counter()
    session.execute(text("DROP INDEX IF EXISTS ix_images_embedding_hnsw"))
    now = datetime.datetime.now(tz=datetime.UTC)
    session.execute(
        text(
            "INSERT INTO devices (id, volume_identity, volume_kind, label, "
            "first_seen_at, last_seen_at) VALUES (:id, :identity, "
            "'windows-volume-guid', 'HD-BENCH', :now, :now) ON CONFLICT DO NOTHING"
        ),
        {"id": str(BENCH_DEVICE), "identity": "\\\\?\\Volume{bench-040}\\", "now": now},
    )
    statement = text(
        "INSERT INTO images (id, device_id, relative_path, filename, extension, "
        "embedding, latitude, longitude, position_source, captured_at, "
        "capture_source) "
        "SELECT gen_random_uuid(), :device, 'bench/' || (:offset + n) || '.jpg', "
        "'bench', 'jpg', CAST(:vector AS vector), lat, lon, "
        "CASE WHEN lat IS NULL THEN 'unknown' ELSE 'exif_gps' END, captured, "
        "CASE WHEN captured IS NULL THEN 'unknown' ELSE 'exif_original' END "
        "FROM unnest(CAST(:lats AS double precision[]), "
        "CAST(:lons AS double precision[]), CAST(:dates AS timestamp[])) "
        "WITH ORDINALITY AS t(lat, lon, captured, n)"
    )
    chunk = 10_000
    for offset in range(0, len(planned), chunk):
        part = planned[offset : offset + chunk]
        session.execute(
            statement,
            {
                "device": str(BENCH_DEVICE),
                "offset": offset,
                "vector": vector,
                "lats": [
                    row["position"][0] if row["position"] else None for row in part
                ],
                "lons": [
                    row["position"][1] if row["position"] else None for row in part
                ],
                "dates": [row["captured"] for row in part],
            },
        )
    session.commit()
    session.execute(text("ANALYZE images"))
    session.commit()
    print(
        f"seeded {len(planned)} rows in {time.perf_counter() - began:.1f} s",
        flush=True,
    )
    return len(planned)


def unit_vector(seed: int) -> list[float]:
    generator = random.Random(seed)
    values = [generator.gauss(0, 1) for _ in range(512)]
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


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
        """Click the first enabled element of `selector` -- with this text at
        its start, when one is given -- as `element.click()` does."""
        wanted = (
            f"e.textContent.trim().startsWith({json.dumps(text)}) && !e.disabled"
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

    def mouse(self, kind: str, x: float, y: float, **extra: Any) -> None:
        self.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, **extra)

    def press(self, x: float, y: float) -> None:
        """A click of the mouse at a point of the viewport."""
        self.mouse("mouseMoved", x, y)
        self.mouse("mousePressed", x, y, button="left", buttons=1, clickCount=1)
        self.mouse("mouseReleased", x, y, button="left", buttons=0, clickCount=1)

    def drag(self, start: tuple[float, float], end: tuple[float, float]) -> None:
        self.mouse("mouseMoved", *start)
        self.mouse("mousePressed", *start, button="left", buttons=1, clickCount=1)
        for step in range(1, 11):
            x = start[0] + (end[0] - start[0]) * step / 10
            y = start[1] + (end[1] - start[1]) * step / 10
            self.mouse("mouseMoved", x, y, button="left", buttons=1)
            time.sleep(0.016)
        self.mouse("mouseReleased", *end, button="left", buttons=0, clickCount=1)

    def wheel(self, x: float, y: float, delta_y: float) -> None:
        self.mouse("mouseWheel", x, y, deltaX=0, deltaY=delta_y)

    def text(self) -> str:
        return str(self.evaluate("document.body ? document.body.innerText : ''") or "")

    def path(self) -> str:
        return str(self.evaluate("location.pathname + location.search") or "")

    def rect(self, selector: str) -> dict[str, float] | None:
        value = self.evaluate(
            f"(() => {{ const e = document.querySelector({json.dumps(selector)});"
            " if (!e) return null; const r = e.getBoundingClientRect();"
            " return {left: r.left, top: r.top, width: r.width, height: r.height}; })()"
        )
        return dict(value) if value else None

    def screenshot(self, file: Path) -> bytes:
        data = base64.b64decode(
            self.call("Page.captureScreenshot", format="png")["data"]
        )
        file.write_bytes(data)
        return data

    def requests(self, since: int = 0) -> list[dict[str, Any]]:
        return [
            event["params"]
            for event in self.events[since:]
            if event.get("method") == "Network.requestWillBeSent"
        ]

    def extra_headers(self, since: int = 0) -> dict[str, dict[str, Any]]:
        """The headers each request carried on the wire, by request id."""
        return {
            event["params"]["requestId"]: dict(event["params"].get("headers", {}))
            for event in self.events[since:]
            if event.get("method") == "Network.requestWillBeSentExtraInfo"
        }

    def close(self) -> None:
        try:
            self._socket.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# A window
# --------------------------------------------------------------------------


class Window:
    def __init__(self, m: Measurement, label: str, switches: Sequence[str]) -> None:
        self.m = m
        self.label = label
        self.offset = m.shell_log.stat().st_size if m.shell_log.exists() else 0
        self._output = (m.out / f"{label}.electron.txt").open("wb")
        self.process = subprocess.Popen(
            [
                str(ELECTRON),
                OCCLUSION_OFF,
                *switches,
                f"--remote-debugging-port={DEBUG_PORT}",
                str(FRONTEND),
            ],
            cwd=FRONTEND,
            env=m.environment(),
            stdin=subprocess.DEVNULL,
            stdout=self._output,
            stderr=self._output,
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

        found, _ = wait_until(seen, seconds, POLL_SECONDS)
        if found is None:
            raise CheckFailedError(f"{self.label}: no mark {name} in {seconds:.0f} s")
        return str(found)

    def map_mark(self, since: int) -> dict[str, int]:
        detail = self.wait_mark("map", since, MARK_SECONDS)
        found = MAP_MARK.search(detail)
        if found is None:
            raise CheckFailedError(f"a map mark that does not parse: {detail!r}")
        return {key: int(value) for key, value in found.groupdict().items()}

    def search_mark(self, since: int) -> dict[str, Any]:
        detail = self.wait_mark("search", since, MARK_SECONDS)
        found = SEARCH_MARK.search(detail)
        if found is None:
            raise CheckFailedError(f"a search mark that does not parse: {detail!r}")
        return dict(found.groupdict())

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
        self.cdp.call("Page.enable")
        return self.cdp

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
        self._output.close()


# --------------------------------------------------------------------------
# The measurement
# --------------------------------------------------------------------------


class Measurement:
    def __init__(
        self,
        work: Path,
        dji: Path,
        offline: str,
        drag_minutes: float,
        skip_browser: bool = False,
    ) -> None:
        self.work = work.resolve()
        self.dji = dji
        self.offline = offline
        self.drag_minutes = drag_minutes
        self.skip_browser = skip_browser
        self.local = self.work / "Local"
        self.data = self.local / "SolidVision-dev"
        self.logs = self.data / "logs"
        self.shell_log = self.logs / "shell.log"
        self.out = self.work / "out"
        self.corpora = self.work / "corpora"
        for directory in (self.local, self.out):
            directory.mkdir(parents=True, exist_ok=True)
        self.window: Window | None = None
        self.url = ""
        self.results: list[tuple[str, str]] = []
        self.framings: set[str] = set()
        self.dji_cell: dict[str, float] | None = None
        self.busiest: tuple[float, float] | None = None

    def environment(self) -> dict[str, str]:
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
        )
        return environment

    def open(self, label: str, *switches: str) -> Window:
        if self.window is not None:
            self.window.close()
            self.window = None
        self.window = Window(self, label, switches)
        self.window.wait_mark("home")
        self.url = self.window.url()
        self.window.connect()
        return self.window

    @property
    def cdp(self) -> Cdp:
        if self.window is None or self.window.cdp is None:
            raise CheckFailedError("no window")
        return self.window.cdp

    @property
    def win(self) -> Window:
        if self.window is None:
            raise CheckFailedError("no window")
        return self.window

    def busiest_place(self) -> tuple[float, float]:
        """The place the seed weighs most: the plan's first, from its fixed
        seed, whether this run wrote the rows or an earlier one did."""
        if self.busiest is None:
            places, _ = seed_plan(1)
            self.busiest = places[0]
        return self.busiest

    def ready(self, seconds: float = 300) -> None:
        """The search's models loaded, as RFC-038's measurement waited: on two
        cores, rounds timed while they load measure the loading (§14.1)."""

        def answered() -> bool:
            _, body = http(self.url, "/health")
            return isinstance(body, dict) and body.get("search") == "ready"

        found, _ = wait_until(answered, seconds, 0.5)
        if not found:
            raise CheckFailedError(f"/health never said search: ready in {seconds} s")

    def record(self, name: str, result: str) -> None:
        self.results.append((name, result))
        say(f"RESULT {name}: {result}")

    def goto(self, path: str) -> None:
        """The page at this path, loaded anew: an empty cache of TanStack Query,
        as a window that just opened has."""
        match = re.search(r"centro=([-\d.]+),([-\d.]+)", path)
        if match:
            self.framings.add(f"{match[1]},{match[2]}")
        self.cdp.call("Page.navigate", url=self.url + path)
        wait_until(
            lambda: self.cdp.evaluate("document.readyState") == "complete", 30, 0.1
        )

    def background(self, on: bool) -> None:
        """'Fundo do OpenStreetMap', as the page stores it, before the next load."""
        value = "on" if on else "off"
        self.cdp.evaluate(
            f"localStorage.setItem({json.dumps(OSM_STORAGE_KEY)}, '{value}')"
        )

    def map_answer(
        self, box: tuple[float, float, float, float], precision: int, **extra: Any
    ) -> dict[str, Any]:
        query = {
            "min_lat": box[0],
            "min_lon": box[1],
            "max_lat": box[2],
            "max_lon": box[3],
            "precision": precision,
            **extra,
        }
        status, body = http(
            self.url, "/api/v1/images/map?" + urllib.parse.urlencode(query, doseq=True)
        )
        if status != 200:
            raise CheckFailedError(f"GET /images/map {status}: {body}")
        return dict(body)

    def corpus(self) -> Path:
        from dataset_tools.manifest import (
            DEMO_CORPUS_ROOT,
            DEMO_MANIFEST_PATH,
            load_manifest,
        )
        from dataset_tools.materialize import materialize

        source = self.corpora / "demo"
        if not source.exists():
            materialize(load_manifest(DEMO_MANIFEST_PATH), DEMO_CORPUS_ROOT, source)
        return source

    def index(self, folders: Sequence[Path], label: str) -> int:
        """Register the volume that holds the folders and index them, through
        the API."""
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
        mount = Path(holding["mount_point"])
        scopes = [folder.resolve().relative_to(mount).as_posix() for folder in folders]
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

    def container(self) -> dict[str, float]:
        found, _ = wait_until(lambda: self.cdp.rect(".leaflet-container"), 30, 0.1)
        if not found:
            raise CheckFailedError(f"no map on the screen: {self.cdp.text()[:300]!r}")
        return dict(found)

    def pixel_of(self, lat: float, lon: float) -> tuple[float, float]:
        """The viewport point of a position, from the URL's framing and the
        map's rectangle."""
        found = re.search(r"centro=([-\d.]+),([-\d.]+)&zoom=(\d+)", self.cdp.path())
        if not found:
            raise CheckFailedError(f"no framing in the URL: {self.cdp.path()}")
        centre_lat, centre_lon, zoom = float(found[1]), float(found[2]), int(found[3])
        box = self.container()
        cx, cy = project(centre_lat, centre_lon, zoom)
        px, py = project(lat, lon, zoom)
        return (
            box["left"] + box["width"] / 2 + px - cx,
            box["top"] + box["height"] / 2 + py - cy,
        )

    def wait_framing(self) -> str:
        found, _ = wait_until(
            lambda: (path := self.cdp.path()).startswith("/mapa?centro=") and path,
            30,
            0.1,
        )
        if not found:
            raise CheckFailedError(f"the map wrote no framing: {self.cdp.path()}")
        match = re.search(r"centro=([-\d.]+),([-\d.]+)", str(found))
        if match:
            self.framings.add(f"{match[1]},{match[2]}")
        return str(found)

    def open_map(self) -> dict[str, int]:
        """From the home screen, reloaded, a click on "Mapa": the mark."""
        self.goto("/")
        wait_until(lambda: "Busque por descrição" in self.cdp.text(), 60, 0.1)
        since = self.win.position()
        self.cdp.click("header a", "Mapa")
        mark = self.win.map_mark(since)
        self.wait_framing()
        return mark


def colour_near(
    pixel: tuple[int, ...], target: tuple[int, int, int], tolerance: int = 12
) -> bool:
    return all(
        abs(int(pixel[index]) - target[index]) <= tolerance for index in range(3)
    )


def hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.strip().lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------


def check_prepare(m: Measurement) -> None:
    """The dataset and the 40 DJI photos indexed, and a warm window."""
    if not m.dji.is_dir():
        raise CheckFailedError(f"no DJI folder at {m.dji}")
    demo = m.corpus()
    m.open("prepare")
    _, listed = http(m.url, "/api/v1/devices")
    known = sum(device["indexed_images"] for device in listed["devices"])
    if known >= 85:
        say(f"{known} photos already indexed in {m.work}")
    else:
        count = m.index([demo, m.dji], "Demo")
        if count != 85:
            raise CheckFailedError(f"{count} photos indexed, not 85")
    brazil = m.map_answer((-34, -74, 6, -28), 2)
    if not brazil["cells"]:
        raise CheckFailedError("no cell with a position: the DJI photos have none?")
    fullest = max(brazil["cells"], key=lambda cell: cell["count"])
    m.dji_cell = {"latitude": fullest["latitude"], "longitude": fullest["longitude"]}
    m.open("main")
    unplaced = brazil["excluded_unknown_position"]
    m.record(
        "prepare",
        f"85 photos indexed ({unplaced} without a position); "
        f"{len(brazil['cells'])} cells of ~1 km in Brazil, the fullest with "
        f"{fullest['count']} photos at {fullest['latitude']}, {fullest['longitude']}; "
        f"the window at {m.url}",
    )


def sent_tile_headers(netlog: Path) -> list[dict[str, str]]:
    """The headers each request to the tile host carried on the wire, from
    Chromium's network log (`--log-net-log`), one event per line: HTTP/1.1,
    HTTP/2 and QUIC alike. A request through Electron's `webRequest` hook
    reports no raw headers to the debugging protocol (section 14.1)."""
    found: list[dict[str, str]] = []
    if not netlog.exists():
        return found
    for line in netlog.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip().rstrip(",")
        if TILE_HOST not in line or '"headers"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        params = event.get("params") if isinstance(event, dict) else None
        headers = params.get("headers") if isinstance(params, dict) else None
        if not isinstance(headers, list):
            continue
        fields: dict[str, str] = {}
        for header in headers:
            name, _, value = str(header).partition(": ")
            fields[name.lower().lstrip(":")] = value
        if (fields.get("host") or fields.get("authority")) == TILE_HOST:
            found.append(fields)
    return found


def check_tiles(m: Measurement) -> None:
    """Once, with the network on and the map left alone: the tiles, the
    User-Agent and the Referer they carried on the wire, the attribution in
    view and uncovered, and its link in the default browser.

    In a window of its own, with Chromium's network log on, which is complete
    when the window exits: the debugging protocol does not see the headers of
    a request Electron's `webRequest` hook touched."""
    if not resolves(TILE_HOST):
        raise CheckFailedError(f"{TILE_HOST} does not resolve: no network?")
    netlog = m.out / "tiles.netlog.json"
    netlog.unlink(missing_ok=True)
    m.open("tiles", f"--log-net-log={netlog}")
    cdp = m.cdp
    # A tile from the HTTP cache never goes on the wire: the profile's cache
    # outlives the window, and the first run of this check found every tile
    # there.
    cdp.call("Network.clearBrowserCache")
    m.goto("/")
    m.background(True)
    since = len(cdp.events)
    mark = m.open_map()
    loaded, _ = wait_until(
        lambda: cdp.evaluate(
            "document.querySelectorAll('img.leaflet-tile-loaded').length"
        ),
        60,
        0.2,
    )
    if not loaded:
        raise CheckFailedError("no tile loaded in 60 s")
    cdp.pump(2)
    tiles = [r for r in cdp.requests(since) if TILE_HOST in r["request"]["url"]]
    declared = {
        str(r["request"]["headers"]["User-Agent"])
        for r in tiles
        if r["request"]["headers"].get("User-Agent")
    }
    # The debugging protocol's raw headers, a second reading: over HTTP/2 and
    # HTTP/3 the names are lower case, which the first version of this check
    # missed (section 14.1).
    raw = cdp.extra_headers(since)
    raw_agents = {
        str(value)
        for r in tiles
        for name, value in raw.get(r["requestId"], {}).items()
        if name.lower() == "user-agent"
    }
    cached = sum(
        1
        for event in cdp.events[since:]
        if event.get("method") == "Network.requestServedFromCache"
    )
    attribution = cdp.rect(".leaflet-control-attribution")
    legend = cdp.rect("[class*='legend']")
    box = m.container()
    if attribution is None or legend is None:
        raise CheckFailedError(f"attribution {attribution}, legend {legend}")
    in_corner = (
        abs(attribution["left"] + attribution["width"] - (box["left"] + box["width"]))
        < 2
        and abs(
            attribution["top"] + attribution["height"] - (box["top"] + box["height"])
        )
        < 2
    )
    overlap = not (
        legend["left"] + legend["width"] <= attribution["left"]
        or attribution["left"] + attribution["width"] <= legend["left"]
        or legend["top"] + legend["height"] <= attribution["top"]
        or attribution["top"] + attribution["height"] <= legend["top"]
    )
    words = str(
        cdp.evaluate("document.querySelector('.leaflet-control-attribution').innerText")
    )
    cdp.screenshot(m.out / "tiles.png")
    before = cdp.path()
    checks = {
        "the attribution in the corner below on the right": in_corner,
        "the legend does not cover it": not overlap,
        f"its words ({words!r})": "colaboradores do OpenStreetMap" in words,
    }
    if m.skip_browser:
        link = "its link not clicked (--skip-browser)"
    else:
        position = m.win.position()
        cdp.click(".leaflet-control-attribution a")
        opened, _ = wait_until(
            lambda: any(
                "in the default browser" in line for line in m.win.lines(position)
            ),
            15,
            0.1,
        )
        time.sleep(2)
        checks["the browser asked to open the link"] = bool(opened)
        checks["the window stayed where it was"] = cdp.path() == before
        link = f"its link opened the default browser, the window stayed at {before}"
    m.win.close()
    m.window = None
    sent = sent_tile_headers(netlog)
    agents = {fields["user-agent"] for fields in sent if "user-agent" in fields}
    referers = {fields["referer"] for fields in sent if "referer" in fields}
    checks[f"the window's User-Agent on the wire ({sorted(agents)})"] = bool(
        sent
    ) and agents == {WINDOW_UA}
    checks[f"the same in the debugging protocol ({sorted(raw_agents)})"] = (
        raw_agents <= {WINDOW_UA}
    )
    m.open("main")
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise CheckFailedError(
            f"{failed}; {len(tiles)} requests, {cached} from the cache, "
            f"{len(sent)} sets of headers in the network log"
        )
    m.record(
        "tiles",
        f"{loaded} tiles loaded, {len(tiles)} requests to {TILE_HOST} ({cached} from "
        f"the cache); {len(sent)} sets of headers sent to it in the network log, "
        f"all with the User-Agent {sorted(agents)}, as the debugging protocol's raw "
        f"headers ({sorted(raw_agents)}); the page declared {sorted(declared)[:1]}; "
        f"the Referer {sorted(referers)}; the attribution "
        f"{words!r} in the corner, uncovered by the legend; {link}; the map mark "
        f"{mark}",
    )


def check_offline(m: Measurement) -> None:
    """The first criterion: with the network off, the map opens and shows the
    cells over the outline of the states, the layer paused with its strip, and
    no tile path in shell.log."""
    units = load_units()
    switches: list[str] = []
    if m.offline == "simulated":
        switches.append(SIMULATED_OFFLINE)
        m.open("offline", *switches)
    else:
        ask(
            "Medição do RFC-040: desligue a rede agora (o Wi-Fi e o cabo) e clique OK. "
            "A janela do SolidVision vai abrir o mapa sem rede."
        )

        def network_gone() -> bool:
            # The cache of Windows' DNS client answers for a while after the
            # network is gone.
            flush_dns()
            return not resolves(TILE_HOST)

        gone, _ = wait_until(network_gone, NETWORK_SECONDS, 2)
        if not gone:
            raise CheckFailedError(f"{TILE_HOST} still resolves: the network is on")
    cdp = m.cdp
    cdp.call("Network.clearBrowserCache")
    m.goto("/")
    m.background(True)
    since = len(cdp.events)
    position = m.win.position()
    mark = m.open_map()
    paused, _ = wait_until(lambda: OFFLINE_TEXT in cdp.text(), 60, 0.2)
    tiles_on_map = cdp.evaluate("document.querySelectorAll('img.leaflet-tile').length")
    path = cdp.path()
    found = re.search(r"centro=([-\d.]+),([-\d.]+)&zoom=(\d+)", path)
    assert found is not None
    cdp.screenshot(m.out / "offline.png")
    # The legend, the controls and Leaflet's own are drawn over the map, and
    # could be over a unit's point: hidden for the screenshot that is read.
    cdp.evaluate(
        "document.querySelectorAll(\"[class*='legend'], [class*='controls'], "
        ".leaflet-control-container\").forEach(e => { e.style.visibility = 'hidden'; })"
    )
    shot = cdp.screenshot(m.out / "offline-read.png")
    cdp.evaluate(
        "document.querySelectorAll(\"[class*='legend'], [class*='controls'], "
        ".leaflet-control-container\").forEach(e => { e.style.visibility = ''; })"
    )
    from PIL import Image

    image = Image.open(io.BytesIO(shot)).convert("RGB")
    ratio = float(cdp.evaluate("window.devicePixelRatio") or 1)

    def pixel(lat: float, lon: float) -> tuple[int, ...]:
        x, y = m.pixel_of(lat, lon)
        value = image.getpixel((round(x * ratio), round(y * ratio)))
        if not isinstance(value, tuple):
            raise CheckFailedError(f"no pixel at {lat}, {lon}: {value}")
        return tuple(int(channel) for channel in value)

    colours = cdp.evaluate(
        "(() => { const s = getComputedStyle(document.documentElement);"
        " return ['--color-map-land', '--color-surface-muted'].map(n =>"
        " s.getPropertyValue(n).trim()); })()"
    )
    land, water = hex_rgb(colours[0]), hex_rgb(colours[1])
    answer_box = m.map_answer((-90, -180, 90, 180), mark["applied"])
    cells = answer_box["cells"]
    step = 10 ** -mark["applied"]

    def in_cell(lat: float, lon: float) -> bool:
        return any(
            abs(cell["latitude"] - lat) <= step / 2
            and abs(cell["longitude"] - lon) <= step / 2
            for cell in cells
        )

    unit_results = []
    for unit in units:
        under = pixel(unit.inner_lat, unit.inner_lon)
        ok = in_cell(unit.inner_lat, unit.inner_lon) or colour_near(under, land)
        unit_results.append((unit.sigla, ok, under))
    sea = pixel(-20.0, -33.0)
    drawn_cells = []
    for cell in cells:
        under = pixel(cell["latitude"], cell["longitude"])
        drawn_cells.append(
            not colour_near(under, land) and not colour_near(under, water)
        )
    log_lines = m.win.lines(position)
    tile_paths = [line for line in log_lines if TILE_PATH.search(line)]
    tile_lines = [line for line in log_lines if TILE_HOST in line]
    network_errors = [
        event["params"].get("errorText")
        for event in cdp.events[since:]
        if event.get("method") == "Network.loadingFailed"
    ]
    # Simulated, the script's own resolver still answers: what counts is that
    # the window's tile requests failed to resolve.
    host_failed = (
        not resolves(TILE_HOST)
        if m.offline == "person"
        else "net::ERR_NAME_NOT_RESOLVED" in network_errors
    )
    checks = {
        "the tile host does not resolve": host_failed,
        "the strip of the pause": bool(paused),
        "no tile on the map": tiles_on_map == 0,
        f"the cells drawn ({mark['cells']} in the mark, {len(cells)} in the API)": mark[
            "cells"
        ]
        == len(cells)
        and all(drawn_cells),
        "the 27 units beneath: land at a point inside each": len(unit_results) == 27
        and all(ok for _, ok, _ in unit_results),
        f"the sea is the sea ({sea})": colour_near(sea, water),
        "no tile path in shell.log": not tile_paths,
    }
    failed = [name for name, ok in checks.items() if not ok]
    missing = [(sigla, pixel) for sigla, ok, pixel in unit_results if not ok]
    if m.offline == "person":
        ask("Pronto: religue a rede agora e clique OK.")
        back, _ = wait_until(lambda: resolves(TILE_HOST), NETWORK_SECONDS, 2)
        if back:
            resumed, _ = wait_until(lambda: OFFLINE_TEXT not in cdp.text(), 60, 0.5)
            say(
                "the network is back; the strip left on the window's 'online': "
                f"{bool(resumed)}"
            )
        else:
            say("the network did not come back in 5 minutes")
    else:
        m.open("main")
    if failed:
        raise CheckFailedError(f"{failed}; units {missing}; shell.log {tile_paths[:3]}")
    how = (
        "the network off by the person"
        if m.offline == "person"
        else "SIMULATED: the window alone could not resolve the tile host"
    )
    m.record(
        "offline",
        f"{how}; the map at {path}: the mark {mark}; the strip of the pause, no tile "
        f"on the map; the {len(cells)} cells of the response on the screen; land "
        f"under a point of each of the 27 units; the sea {sea}; {len(tile_lines)} "
        "lines of shell.log name the tile host, none with a path; the page's failed "
        f"loads {sorted(set(e for e in network_errors if e))}",
    )


def check_cell(m: Measurement) -> None:
    """From a cell of the DJI photos to the search: with a text search in the
    map's URL, and without one."""
    assert m.dji_cell is not None
    cdp = m.cdp
    lat, lon = m.dji_cell["latitude"], m.dji_cell["longitude"]
    m.goto("/")
    m.background(False)
    results = []
    for carried in ("telhado", None):
        query = f"&q={carried}" if carried else ""
        m.goto(f"/mapa?centro={lat},{lon}&zoom=12{query}")
        drawn, _ = wait_until(
            lambda: cdp.evaluate("!!document.querySelector('[class*=legend]')"), 60, 0.2
        )
        if not drawn:
            raise CheckFailedError(f"no cells drawn: {cdp.text()[:300]!r}")
        time.sleep(1)
        x, y = m.pixel_of(lat, lon)
        cdp.press(x, y)
        balloon, _ = wait_until(
            lambda: cdp.evaluate(
                "(() => { const p = document.querySelector('.leaflet-popup');"
                " return p ? p.innerText : null; })()"
            ),
            10,
            0.1,
        )
        if not balloon:
            raise CheckFailedError("no balloon after the click on the cell")
        cdp.screenshot(m.out / f"balloon-{carried or 'none'}.png")
        since = m.win.position()
        cdp.click(".leaflet-popup button", "Buscar")
        if carried:
            arrived, _ = wait_until(lambda: cdp.path().startswith("/busca?"), 30, 0.1)
            if not arrived:
                raise CheckFailedError(f"not at /busca: {cdp.path()}")
            mark = m.win.search_mark(since)
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(cdp.path()).query)
            near_lat, near_lon = (float(v) for v in params["perto"][0].split(","))
            radius = float(params["raio"][0])
            status, body = http(
                m.url,
                "/api/v1/images/search?"
                + urllib.parse.urlencode(
                    {
                        "q": carried,
                        "limit": 40,
                        "near_lat": near_lat,
                        "near_lon": near_lon,
                        "radius_m": radius,
                    }
                ),
            )
            if status != 200:
                raise CheckFailedError(f"the search: {status} {body}")
            inside = all(
                r["latitude"] is not None
                and haversine_m(near_lat, near_lon, r["latitude"], r["longitude"])
                <= radius
                for r in body["results"]
            )
            if "perto_de" in params or not inside or not body["results"]:
                raise CheckFailedError(
                    f"{params}; {len(body['results'])} results, inside {inside}"
                )
            results.append(
                f"with “{carried}”: the balloon said {balloon.splitlines()!r}, and "
                f"/busca came with perto={params['perto'][0]} raio={radius:g} and no "
                f"perto_de; {mark['results']} results, every one inside the circle "
                f"(the search mark: response {mark['response']} ms, painted "
                f"{mark['painted']} ms)"
            )
        else:
            arrived, _ = wait_until(lambda: cdp.path().startswith("/?"), 30, 0.1)
            if not arrived:
                raise CheckFailedError(f"not at the home screen: {cdp.path()}")
            time.sleep(1)
            chip = "do ponto do mapa" in cdp.text()
            focused = cdp.evaluate(
                "(() => { const a = document.activeElement;"
                " return a && a.tagName === 'INPUT' ? a.placeholder : null; })()"
            )
            if not chip or focused != "Descreva o que procurar perto do ponto do mapa…":
                raise CheckFailedError(
                    f"chip {chip}, focus {focused!r}: {cdp.text()[:300]!r}"
                )
            pills = re.findall(r"Até [^\n]* do ponto do mapa", cdp.text())
            results.append(
                f"without a search: the balloon said {balloon.splitlines()!r}, and the "
                f"home screen came at {cdp.path()} with {pills} and the bar focused, "
                f"its text {focused!r}"
            )
    # The list: the keyboard's way to the same cell.
    m.goto(f"/mapa?centro={lat},{lon}&zoom=12")
    wait_until(
        lambda: cdp.evaluate("!!document.querySelector('[class*=legend]')"), 60, 0.2
    )
    cdp.click("button", "Lista")
    listed, _ = wait_until(
        lambda: cdp.evaluate("!!document.querySelector('aside ol li button')"), 10, 0.1
    )
    if not listed:
        raise CheckFailedError(f"no row in the list: {cdp.text()[:300]!r}")
    cdp.click("aside ol li button")
    from_list, _ = wait_until(
        lambda: cdp.evaluate("!!document.querySelector('.leaflet-popup')"), 10, 0.1
    )
    cdp.screenshot(m.out / "list.png")
    if not from_list:
        raise CheckFailedError("the list's row opened no balloon")
    results.append("the list's first row opened the balloon")
    m.record("cell", "; ".join(results))


def _timed(m: Measurement, label: str) -> list[dict[str, int]]:
    """Five openings of the map from the home screen, with the background off,
    the search's models loaded."""
    m.ready()
    m.goto("/")
    m.background(False)
    marks = []
    for _ in range(RUNS):
        marks.append(m.open_map())
    say(
        f"{label}: painted {ms([x['painted'] for x in marks])}; response "
        f"{ms([x['response'] for x in marks])}; cells {[x['cells'] for x in marks]}, "
        f"precision {sorted({(x['requested'], x['applied']) for x in marks})}"
    )
    return marks


def check_brazil(m: Measurement) -> None:
    """The second criterion: all of Brazil, from the click on "Mapa" to the
    cells drawn, with the dataset and the 40 DJI photos."""
    marks = _timed(m, "dataset and DJI")
    m.cdp.screenshot(m.out / "brazil.png")
    m.record(
        "brazil",
        f"{ms([x['painted'] for x in marks])} to the cells drawn; the response "
        f"{ms([x['response'] for x in marks])}; {marks[0]['cells']} cells at precision "
        f"{marks[0]['requested']}->{marks[0]['applied']}, at {m.cdp.path()}",
    )


def check_seed(m: Measurement) -> None:
    """The 100 000 rows, written into the work directory's cluster with the
    window closed."""
    if m.window is not None:
        m.window.close()
        m.window = None
    began = time.perf_counter()
    written = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.infrastructure.database.embedded",
            "run",
            "--",
            sys.executable,
            str(Path(__file__).resolve()),
            "--seed-rows",
            str(SEED_ROWS),
        ],
        cwd=BACKEND,
        env={**m.environment(), "PYTHONPATH": str(BACKEND)},
        capture_output=True,
        text=True,
        timeout=1800,
        creationflags=CREATE_NO_WINDOW,
    )
    if written.returncode != 0:
        raise CheckFailedError(f"the rows were not written: {written.stderr[-600:]}")
    say(written.stdout.strip())
    places, _ = seed_plan(1)
    m.busiest = places[0]
    m.open("big")
    brazil = m.map_answer((-34, -74, 6, -28), 0)
    photos = sum(cell["count"] for cell in brazil["cells"])
    m.record(
        "seed",
        f"{SEED_ROWS} rows in {time.perf_counter() - began:.0f} s with the window "
        f"closed ({written.stdout.strip()}); all of Brazil at precision 0: "
        f"{len(brazil['cells'])} cells, {photos} photos, "
        f"{brazil['excluded_unknown_position']} without a position; the busiest "
        f"place at {places[0][0]:.4f}, {places[0][1]:.4f}",
    )


def check_brazil_big(m: Measurement) -> None:
    """The third criterion: the same, with the 100 000 photos."""
    marks = _timed(m, "100 000 photos")
    m.cdp.screenshot(m.out / "brazil-100k.png")
    m.record(
        "brazil_big",
        f"{ms([x['painted'] for x in marks])} to the cells drawn; the response "
        f"{ms([x['response'] for x in marks])}; {marks[0]['cells']} cells at precision "
        f"{marks[0]['requested']}->{marks[0]['applied']}",
    )


def check_zoom(m: Measurement) -> None:
    """The fourth: a step of the wheel from zoom 6 to 7 over the busiest area,
    with the 100 000: from the end of the zoom to the new cells."""
    busiest = m.busiest_place()
    lat, lon = round(busiest[0], 4), round(busiest[1], 4)
    cdp = m.cdp
    m.ready()
    m.goto("/")
    m.background(False)
    marks = []
    for _ in range(RUNS):
        m.goto(f"/mapa?centro={lat},{lon}&zoom=6")
        drawn, _ = wait_until(
            lambda: cdp.evaluate("!!document.querySelector('[class*=legend]')"),
            120,
            0.1,
        )
        if not drawn:
            raise CheckFailedError(f"no cells at zoom 6: {cdp.text()[:300]!r}")
        time.sleep(1.5)
        box = m.container()
        since = m.win.position()
        cdp.wheel(box["left"] + box["width"] / 2, box["top"] + box["height"] / 2, -100)
        mark = m.win.map_mark(since)
        zoomed = cdp.path()
        if "zoom=7" not in zoomed or (mark["requested"], mark["applied"]) != (1, 1):
            raise CheckFailedError(f"the wheel gave {zoomed} and {mark}")
        marks.append(mark)
    cdp.screenshot(m.out / "zoom-7.png")
    painted = ms([x["painted"] for x in marks])
    response = ms([x["response"] for x in marks])
    m.record(
        "zoom",
        f"from zoom 6 to 7 at {lat}, {lon}: {painted} from the end of the zoom to "
        f"the new cells; the response {response}; {marks[0]['cells']} cells at "
        "precision 1",
    )


def check_notices(m: Measurement) -> None:
    """With a date filter over the 100 000: the two notices with the API's
    numbers, and "Tirar o filtro de datas" taking the filter off."""
    cdp = m.cdp
    m.goto("/")
    m.background(False)
    m.goto("/mapa?centro=-14.0,-52.0&zoom=4&de=2018-01-01&ate=2020-12-31")
    wait_until(lambda: "não aparece" in cdp.text(), 120, 0.2)
    text = cdp.text()
    answer = m.map_answer(
        (-34, -74, 6, -28),
        0,
        captured_from="2018-01-01",
        captured_to="2021-01-01",
    )
    date = f"{answer['excluded_unknown_date']:,}".replace(",", ".")
    position = f"{answer['excluded_unknown_position']:,}".replace(",", ".")
    has_date = (
        f"{date} fotos não aparecem no mapa porque não têm data de captura." in text
    )
    has_position = (
        f"{position} fotos não aparecem no mapa porque não têm posição." in text
    )
    cdp.screenshot(m.out / "notices.png")
    cdp.click("button", "Tirar o filtro de datas")
    cleared, _ = wait_until(
        lambda: "de=" not in cdp.path() and "data de captura" not in cdp.text(), 60, 0.2
    )
    if not (has_date and has_position and cleared):
        raise CheckFailedError(
            f"date {has_date} ({date}), position {has_position} ({position}), "
            f"cleared {cleared}: {text[:400]!r}"
        )
    m.record(
        "notices",
        f"2018 to 2020: '{date} fotos … data de captura' and '{position} fotos … "
        f"posição', the API's numbers; 'Tirar o filtro de datas' left {cdp.path()}",
    )


def check_balloon_search(m: Measurement) -> None:
    """Information: with the 100 000, the search the balloon opens, from the
    click on "Buscar perto daqui" to the grid, by the `search` mark."""
    busiest = m.busiest_place()
    cdp = m.cdp
    lat, lon = round(busiest[0], 2), round(busiest[1], 2)
    m.ready()
    m.goto("/")
    m.background(False)
    marks = []
    for _ in range(RUNS):
        m.goto(f"/mapa?centro={lat},{lon}&zoom=12&q=telhado")
        wait_until(
            lambda: cdp.evaluate("!!document.querySelector('[class*=legend]')"),
            120,
            0.2,
        )
        time.sleep(1)
        cdp.click("button", "Lista")
        wait_until(
            lambda: cdp.evaluate("!!document.querySelector('aside ol li button')"),
            30,
            0.1,
        )
        since = m.win.position()
        cdp.click("aside ol li button", "Buscar perto daqui")
        marks.append(m.win.search_mark(since))
    m.record(
        "balloon_search",
        f"“telhado” near the busiest cell, the 100 000 in the table: painted "
        f"{ms([float(x['painted']) for x in marks])}, the response "
        f"{ms([float(x['response']) for x in marks])}, {marks[0]['results']} results",
    )


def check_nobackground(m: Measurement) -> None:
    """With "Fundo do OpenStreetMap" off, minutes of drags and zooms by this
    script, and no request to the tile host."""
    cdp = m.cdp
    m.goto("/")
    m.background(True)
    m.goto("/mapa?centro=-14.0,-52.0&zoom=5")
    m.container()
    time.sleep(2)
    cdp.click("label", "Fundo do OpenStreetMap")
    time.sleep(1)
    since = len(cdp.events)
    generator = random.Random(5)
    end = time.perf_counter() + m.drag_minutes * 60
    gestures = 0
    while time.perf_counter() < end:
        box = m.container()
        cx, cy = box["left"] + box["width"] / 2, box["top"] + box["height"] / 2
        if generator.random() < 0.6:
            dx, dy = generator.uniform(-250, 250), generator.uniform(-180, 180)
            cdp.drag((cx, cy), (cx + dx, cy + dy))
        else:
            cdp.wheel(cx, cy, generator.choice((-100, 100)))
        gestures += 1
        cdp.pump(0.6)
    m.wait_framing()
    tiles = [r for r in cdp.requests(since) if TILE_HOST in r["request"]["url"]]
    maps = [
        r for r in cdp.requests(since) if "/api/v1/images/map" in r["request"]["url"]
    ]
    if tiles:
        raise CheckFailedError(f"{len(tiles)} requests to {TILE_HOST}")
    m.record(
        "nobackground",
        f"{m.drag_minutes:g} min, {gestures} drags and zooms, {len(maps)} requests of "
        f"the map and no request to {TILE_HOST}",
    )


def check_logs(m: Measurement) -> None:
    """No area in the logs: the centre of every framing used, and a tile's
    path, in shell.log and api.log; and no refusal of the CSP."""
    shell = m.shell_log.read_text(encoding="utf-8", errors="replace")
    api_log = m.logs / "api.log"
    api = (
        api_log.read_text(encoding="utf-8", errors="replace")
        if api_log.exists()
        else ""
    )
    # A number with fewer than three places -- the -14.0 this script typed --
    # would be found in any log; the map writes up to four.
    precise = re.compile(r"^-?\d+\.\d{3,}$")
    searched = set()
    for framing in sorted(m.framings):
        lat, lon = framing.split(",")
        if precise.match(lat) or precise.match(lon):
            searched.add(framing)
            searched.update(part for part in (lat, lon) if precise.match(part))
    found = [
        f"{part} in {name}"
        for part in sorted(searched)
        for name, text in (("shell.log", shell), ("api.log", api))
        if part in text
    ]
    tiles = [
        name
        for name, text in (("shell.log", shell), ("api.log", api))
        if TILE_PATH.search(text)
    ]
    refusals = [
        line
        for line in shell.splitlines()
        if "Content Security Policy" in line or "Refused to" in line
    ]
    if found or tiles or refusals:
        raise CheckFailedError(f"{found[:6]}; tiles in {tiles}; CSP {refusals[:3]}")
    m.record(
        "logs",
        f"none of {len(searched)} texts of the {len(m.framings)} framings used -- "
        f"{sorted(searched)[:4]}… --, and no tile path, in shell.log "
        f"({len(shell.splitlines())} lines) and api.log ({len(api.splitlines())} "
        "lines); no refusal of the CSP",
    )


def check_look(m: Measurement) -> None:
    """Screenshots for the eye, and nothing else: all of Brazil, the DJI cell
    with its hint and its balloon, the list, and the coarsened legend."""
    assert m.dji_cell is not None
    cdp = m.cdp
    lat, lon = m.dji_cell["latitude"], m.dji_cell["longitude"]
    shots = []
    for on in (True, False):
        m.goto("/")
        m.background(on)
        m.open_map()
        if on:
            wait_until(
                lambda: cdp.evaluate(
                    "document.querySelectorAll('img.leaflet-tile-loaded').length > 8"
                ),
                30,
                0.2,
            )
        time.sleep(1)
        name = f"look-brazil-{'osm' if on else 'outline'}.png"
        cdp.screenshot(m.out / name)
        shots.append(name)
    m.goto(f"/mapa?centro={lat},{lon}&zoom=12&q=telhado")
    wait_until(
        lambda: cdp.evaluate("!!document.querySelector('[class*=legend]')"), 60, 0.2
    )
    time.sleep(1)
    x, y = m.pixel_of(lat, lon)
    cdp.mouse("mouseMoved", x, y)
    time.sleep(0.5)
    cdp.screenshot(m.out / "look-hint.png")
    cdp.press(x, y)
    time.sleep(0.8)
    cdp.screenshot(m.out / "look-balloon.png")
    cdp.click("button", "Lista")
    time.sleep(0.8)
    cdp.screenshot(m.out / "look-list.png")
    m.goto("/busca?q=telhado")
    wait_until(lambda: "Filtros" in cdp.text(), 60, 0.2)
    cdp.click("button", "Filtros")
    time.sleep(0.5)
    cdp.screenshot(m.out / "look-near-note.png")
    shots += [
        "look-hint.png",
        "look-balloon.png",
        "look-list.png",
        "look-near-note.png",
    ]
    m.record("look", f"screenshots in {m.out}: {shots}")


CHECKS: dict[str, Callable[[Measurement], None]] = {
    "prepare": check_prepare,
    "look": check_look,
    "tiles": check_tiles,
    "offline": check_offline,
    "cell": check_cell,
    "brazil": check_brazil,
    "seed": check_seed,
    "brazil_big": check_brazil_big,
    "zoom": check_zoom,
    "notices": check_notices,
    "balloon_search": check_balloon_search,
    "nobackground": check_nobackground,
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
    parser.add_argument("--offline", choices=("person", "simulated"), default="person")
    parser.add_argument("--drag-minutes", type=float, default=5.0)
    parser.add_argument("--skip-browser", action="store_true")
    parser.add_argument("--seed-rows", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.seed_rows is not None:
        seed_rows(args.seed_rows)
        return 0
    if args.list:
        print("\n".join(CHECKS))
        return 0
    if not (FRONTEND / "dist" / "index.html").is_file():
        print("Run `npm run build` in frontend/ first.")
        return 2
    chosen = [name for name in args.only.split(",") if name] or list(CHECKS)
    work = args.work or Path(tempfile.mkdtemp(prefix="rfc040-map-"))
    heading("The machine")
    describe_machine()
    say(f"work directory {work}; the DJI folder {args.dji}; offline {args.offline}")
    m = Measurement(work, args.dji, args.offline, args.drag_minutes, args.skip_browser)
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
                if name in ("prepare", "seed"):
                    break
            except Exception:  # noqa: BLE001
                failed.append(name)
                m.record(name, f"ERROR: {traceback.format_exc()}")
                if name in ("prepare", "seed"):
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
