r"""The address search, measured: the service, and the page (RFC-043 section 9).

    python measure_address_search.py --only service,page > measure.log 2>&1
    python measure_address_search.py --only stall >> measure.log 2>&1

(`measure.log` stands for `measure_address_search.log`.)

    --only service,page,stall   just these
    --work PATH                 the page's work directory (default: a temporary one)
    --keep                      keep it, with the screenshots

`service` asks the public Nominatim for the addresses below through the
product's own adapter -- `NominatimGeocoder`, with its `urllib`, its
User-Agent and its one request per second --, one after the other: about one
request a second, inside the usage policy. Addresses of public places only;
nothing of any collection is sent.

`page` starts the launcher as the window starts it (`--supervised`, from the
repository root, `PYTHONPATH=backend`), over the build -- `npm run build`
first -- with a `LOCALAPPDATA` of its own under the work directory: its own
data root, cluster and logs. The page the API serves is opened in a headless
Edge, driven through Chromium's debugging protocol with the `Cdp` of RFC-040's
`measure_map.py`. **Not the Electron window**: its single-instance lock is the
fixed `userData` of the shell, and a window the person running this has open
would take a second one's place (RFC-037). The page is the same; what the shell
adds -- `shell.log` and its redaction of every query string -- is not measured
here. **Nothing opens on the desktop.**

It types addresses into the map's field and into "Perto de", and reads the
URL, the line under the field, the network, and the logs of the work directory.

`stall` explains a number of `page`, and asks the service nothing: a request
to the API from the page -- `/capabilities`, which costs the server nothing --
timed by the browser as the map screen opens and right after each move, with
the OSM background on and off, and on the home screen. It ran after the other
two, appended to the same log.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
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
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "experiments" / "rfc-040-map"))

from measure_map import (  # noqa: E402
    FRONTEND,
    Cdp,
    CheckFailedError,
    describe_machine,
    heading,
    http,
    say,
    wait_until,
)

from app.infrastructure.geocoding.nominatim_geocoder import (  # noqa: E402
    AddressSearchUnavailableError,
    NominatimGeocoder,
)

PUBLIC_NOMINATIM = "https://nominatim.openstreetmap.org/search"
VERSION = "0.1.0"
PYTHON = REPO / "backend" / ".venv" / "Scripts" / "python.exe"
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
EDGE_PORT = 9343
CREATE_NO_WINDOW = 0x08000000
READY_SECONDS = 300.0
STOP_SECONDS = 60.0

# Every address below is asked once per run, and none twice across the two
# checks: the public service answers a repeated query from a cache near the
# client in ~25 ms, and a run that repeated one would measure that cache. A
# second run within the cache's lifetime measures it too: change the addresses.
ADDRESSES: list[tuple[str, str]] = [
    ("street", "Rua Desembargador Pedro Silva, Criciúma, SC"),
    ("street", "Avenida Getúlio Vargas, Criciúma, SC"),
    ("street", "Rua Seis de Janeiro, Criciúma, SC"),
    ("street", "Rua João Pessoa, Criciúma, SC"),
    ("street", "Rua do Príncipe, Joinville, SC"),
    ("street", "Rua Bocaiúva, Florianópolis, SC"),
    ("street", "Rua Quinze de Novembro, Blumenau, SC"),
    ("street", "Avenida Brasil, Balneário Camboriú, SC"),
    ("city", "Içara, SC"),
    ("city", "Nova Veneza, SC"),
    ("city", "Forquilhinha, SC"),
    ("city", "Cocal do Sul, SC"),
    ("city", "Tubarão, SC"),
    ("city", "Araranguá, SC"),
    ("rural", "Serra do Corvo Branco"),
    ("rural", "Morro dos Conventos, Araranguá"),
    ("rural", "São Bento Baixo, Nova Veneza"),
    ("rural", "Rio Jordão, Siderópolis"),
    ("rural", "Estrada Geral Morro Albino, Criciúma"),
    ("rural", "Cânion Fortaleza, Cambará do Sul"),
    ("nothing", "Avenida Imaginária 12345, Vila Que Não Há"),
    ("nothing", "qwpzx vvbnm"),
]
REPEATED = "  içara,   SC "
EDGE_PROBE = "Rua Lauro Müller, Criciúma, SC"

CRICIUMA = (-28.6775, -49.3697)
JOINVILLE = (-26.3045, -48.8487)
FLORIANOPOLIS = (-27.5954, -48.5480)
ON_THE_MAP: list[tuple[str, tuple[float, float]]] = [
    ("Avenida Universitária, Criciúma, SC", CRICIUMA),
    ("Rua Visconde de Taunay, Joinville, SC", JOINVILLE),
    ("Avenida Mauro Ramos, Florianópolis, SC", FLORIANOPOLIS),
]
NEAR_FILTER = ("Rua Conselheiro João Zanette, Criciúma, SC", CRICIUMA)
NOWHERE = "Rua Fictícia 77777, Cidade Imaginada"
TYPED_COORDINATES = ("-26.3045, -48.8487", JOINVILLE)
WITHIN_M = 15_000.0
# What a person takes to type an address: the map that just opened or moved
# has settled by then. Right after a move, a request to the API waits in the
# browser for the map's own work -- up to ~0.5 s, and seconds while the screen
# opens -- and a lookup sent in that instant would measure the map (§9.2).
TYPING_S = 4.0

MAP_FIELD = 'input[aria-label="Buscar um endereço"]'
NEAR_FIELD = 'input[placeholder="Rua, número, cidade"]'
CENTRE = re.compile(r"centro=([-\d.]+),([-\d.]+)")
NEAR = re.compile(r"perto=([-\d.]+)(?:%2C|,)([-\d.]+)")


def metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6_371_008.8 * math.asin(math.sqrt(h))


def summary(values: list[float]) -> str:
    if not values:
        return "no samples"
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]
    return (
        f"median {statistics.median(ordered):.0f} ms, p95 {p95:.0f} ms, "
        f"min {ordered[0]:.0f}, max {ordered[-1]:.0f}, n={len(ordered)}"
    )


def elapsed_ms(began: float) -> float:
    return (time.perf_counter() - began) * 1000


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------


class Body:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def read(self) -> bytes:
        return self._content

    def __enter__(self) -> Body:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class TimedOpener:
    """`urllib.request.urlopen`, as the adapter calls it, with the exchange timed
    from the request to the last byte of the answer."""

    def __init__(self) -> None:
        self.exchanges: list[tuple[float, str]] = []
        self.user_agents: set[str] = set()

    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> Any:
        self.user_agents.add(str(request.get_header("User-agent")))
        began = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content = response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            self.exchanges.append((elapsed_ms(began), f"HTTP {error.code}"))
            raise
        except OSError as error:
            self.exchanges.append((elapsed_ms(began), type(error).__name__))
            raise
        self.exchanges.append((elapsed_ms(began), f"HTTP {status}"))
        return Body(content)


def check_service(work: Path) -> list[str]:
    opener = TimedOpener()
    geocoder = NominatimGeocoder(PUBLIC_NOMINATIM, VERSION, opener=opener)
    found: dict[str, int] = {}
    failures: list[str] = []
    began = time.perf_counter()
    for kind, address in ADDRESSES:
        try:
            place = geocoder.geocode(address)
        except AddressSearchUnavailableError as error:
            failures.append(f"{address}: {error}")
            say(f"{kind:8} {address!r}: UNAVAILABLE {error}")
            continue
        ms, outcome = opener.exchanges[-1]
        if place is None:
            say(f"{kind:8} {address!r}: {outcome}, {ms:.0f} ms, nothing found")
            continue
        found[kind] = found.get(kind, 0) + 1
        say(
            f"{kind:8} {address!r}: {outcome}, {ms:.0f} ms -> "
            f"{place.position.latitude:.5f}, {place.position.longitude:.5f} "
            f"{place.label[:90]!r}"
        )
    total_s = time.perf_counter() - began
    sent = len(opener.exchanges)
    times = [ms for ms, _ in opener.exchanges]
    geocoder.geocode(REPEATED)
    cached = len(opener.exchanges) == sent
    probe = []
    for _ in range(2):
        fresh = TimedOpener()
        NominatimGeocoder(PUBLIC_NOMINATIM, VERSION, opener=fresh).geocode(EDGE_PROBE)
        probe.append(fresh.exchanges[-1][0])
        say(f"probe    {EDGE_PROBE!r}, by a geocoder with no cache: {probe[-1]:.0f} ms")
        time.sleep(1.1)
    kinds: dict[str, int] = {}
    for kind, _ in ADDRESSES:
        kinds[kind] = kinds.get(kind, 0) + 1
    return [
        f"{sent} requests in {total_s:.1f} s ({sent / total_s:.2f} per second), "
        f"{len(failures)} failed",
        f"latency to the last byte, every request: {summary(times)}",
        (
            f"first request (resolution, connection, TLS): {times[0]:.0f} ms"
            if times
            else "no request"
        ),
        "found: "
        + ", ".join(
            f"{kind} {found.get(kind, 0)} of {count}" for kind, count in kinds.items()
        ),
        f"the repeated address went to the network: {'no' if cached else 'YES'}",
        f"the service's own cache: a new address in {probe[0]:.0f} ms, the same "
        f"one again, by a geocoder with no cache, in {probe[1]:.0f} ms",
        f"User-Agent sent: {sorted(opener.user_agents)}",
        *failures,
    ]


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------


def environment(local: Path) -> dict[str, str]:
    """RFC-040's: this work directory's data root, the embedded cluster, and no
    model loaded -- nothing here searches."""
    kept = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(
            ("ELECTRON_", "SOLIDVISION_", "EMBEDDED_", "LOG_", "THUMBNAIL_", "MODEL_")
        )
        and name.upper() not in ("API_PORT", "DATABASE_SOURCE", "WARM_UP_MODELS")
    }
    kept.update(
        LOCALAPPDATA=str(local),
        DATABASE_SOURCE="embedded",
        WARM_UP_MODELS="false",
        PYTHONPATH=str(REPO / "backend"),
    )
    return kept


class Launcher:
    """`python -m app.launcher --supervised`, until its stdin ends."""

    def __init__(self, local: Path, stderr: IO[bytes]) -> None:
        self.process = subprocess.Popen(
            [str(PYTHON), "-m", "app.launcher", "--supervised"],
            cwd=REPO,
            env=environment(local),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            creationflags=CREATE_NO_WINDOW,
        )
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                self.events.put(json.loads(line))
            except ValueError:
                continue

    def ready(self) -> str:
        end = time.perf_counter() + READY_SECONDS
        while time.perf_counter() < end:
            try:
                event = self.events.get(timeout=1.0)
            except queue.Empty:
                if self.process.poll() is not None:
                    raise CheckFailedError(
                        f"the launcher exited with {self.process.returncode}"
                    ) from None
                continue
            say(f"launcher: {json.dumps(event)[:160]}")
            if event.get("event") == "ready":
                return str(event["url"]).rstrip("/")
            if event.get("event") in ("failed", "already_running"):
                raise CheckFailedError(f"the launcher said {event}")
        raise CheckFailedError(f"no ready in {READY_SECONDS:.0f} s")

    def stop(self) -> None:
        if self.process.poll() is None:
            assert self.process.stdin is not None
            self.process.stdin.close()
            try:
                self.process.wait(STOP_SECONDS)
            except subprocess.TimeoutExpired:
                say(f"the launcher did not stop in {STOP_SECONDS:.0f} s; killed")
                self.process.kill()
        say(f"launcher stopped with {self.process.returncode}")


class Page:
    def __init__(self, url: str, profile: Path) -> None:
        self.url = url
        self.process = subprocess.Popen(
            [
                str(EDGE),
                "--headless=new",
                f"--remote-debugging-port={EDGE_PORT}",
                f"--user-data-dir={profile}",
                "--window-size=1280,800",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                url + "/",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def target() -> str | None:
            status, targets = http(f"http://127.0.0.1:{EDGE_PORT}", "/json/list")
            if status != 200:
                return None
            for each in targets:
                if each.get("type") == "page" and str(each.get("url", "")).startswith(
                    url
                ):
                    return str(each["webSocketDebuggerUrl"])
            return None

        ws_url, _ = wait_until(target, 60)
        if ws_url is None:
            raise CheckFailedError(f"no page target at {url}")
        self.cdp = Cdp(ws_url)
        self.cdp.call("Network.enable")
        self.cdp.call("Page.enable")

    def goto(self, path: str) -> None:
        self.cdp.call("Page.navigate", url=self.url + path)
        wait_until(
            lambda: self.cdp.evaluate("document.readyState") == "complete", 30, 0.1
        )

    def close(self) -> None:
        self.cdp.close()
        self.process.kill()
        self.process.wait(30)


def wait_for(
    page: Page, what: str, predicate: Callable[[], Any], seconds: float = 30.0
) -> Any:
    value, took = wait_until(predicate, seconds, 0.02)
    if value is None or took is None:
        raise CheckFailedError(
            f"{what}, not in {seconds:.0f} s; the screen says "
            f"{page.cdp.text()[:300]!r}"
        )
    return value


def type_into(page: Page, selector: str, text: str) -> None:
    focused = page.cdp.evaluate(
        f"(() => {{ const e = document.querySelector({json.dumps(selector)});"
        " if (!e) return false; e.focus(); e.select(); return true; })()"
    )
    if not focused:
        raise CheckFailedError(f"no {selector}: {page.cdp.text()[:300]!r}")
    page.cdp.call("Input.insertText", text=text)


def press_enter(page: Page) -> None:
    for kind in ("keyDown", "keyUp"):
        page.cdp.call(
            "Input.dispatchKeyEvent",
            type=kind,
            key="Enter",
            code="Enter",
            windowsVirtualKeyCode=13,
            nativeVirtualKeyCode=13,
        )


def status_lines(page: Page) -> list[str]:
    value = page.cdp.evaluate(
        "[...document.querySelectorAll('[role=status]')].map(e => e.textContent)"
    )
    return [str(line) for line in value or []]


def centre(page: Page) -> tuple[float, float] | None:
    found = CENTRE.search(page.cdp.path())
    return (float(found[1]), float(found[2])) if found else None


def page_now(page: Page) -> float:
    return float(page.cdp.evaluate("performance.now()"))


def moved_at(
    page: Page, expected: tuple[float, float], within_m: float
) -> float | None:
    """The page's clock when its URL put the map at `expected`, or `None`."""
    search, now = page.cdp.evaluate("[location.search, performance.now()]")
    found = CENTRE.search(str(search))
    if found and metres((float(found[1]), float(found[2])), expected) < within_m:
        return float(now)
    return None


def last_lookup(page: Page) -> dict[str, float]:
    """The browser's own timing of the last request to /api/v1/geocode."""
    entry = page.cdp.evaluate(
        "(() => { const e = performance.getEntriesByType('resource')"
        ".filter(e => e.name.includes('/api/v1/geocode')).pop();"
        " return e ? {start: e.startTime, end: e.responseEnd} : null; })()"
    )
    if not entry:
        raise CheckFailedError("no resource timing for /api/v1/geocode")
    return {key: float(value) for key, value in dict(entry).items()}


def geocode_requests(page: Page, since: int) -> int:
    return sum(
        1
        for request in page.cdp.requests(since)
        if "/api/v1/geocode" in str(request.get("request", {}).get("url", ""))
    )


def drive(page: Page, out: Path) -> list[str]:
    results: list[str] = []
    _, capabilities = http(page.url, "/api/v1/capabilities")
    if not isinstance(capabilities, dict) or not capabilities.get("address_search"):
        raise CheckFailedError(f"/capabilities: {capabilities}")

    page.goto("/mapa")
    wait_for(page, "the map's framing", lambda: centre(page))
    wait_for(page, "the address field", lambda: page.cdp.rect(MAP_FIELD))
    page.cdp.screenshot(out / "map-before.png")

    totals, before_request, requests, after_answer = [], [], [], []
    for address, expected in ON_THE_MAP:
        time.sleep(TYPING_S)
        type_into(page, MAP_FIELD, address)
        enter = page_now(page)
        press_enter(page)
        moved = wait_for(
            page,
            f"the map at {address!r}",
            lambda expected=expected: moved_at(page, expected, WITHIN_M),
        )
        line = wait_for(
            page,
            "the line of what was found",
            lambda: next(
                (s for s in status_lines(page) if "© OpenStreetMap" in s), None
            ),
        )
        lookup = last_lookup(page)
        totals.append(moved - enter)
        before_request.append(lookup["start"] - enter)
        requests.append(lookup["end"] - lookup["start"])
        after_answer.append(moved - lookup["end"])
        say(
            f"map {address!r}: there in {moved - enter:.0f} ms -- "
            f"{lookup['start'] - enter:.0f} ms to the request, "
            f"{lookup['end'] - lookup['start']:.0f} ms of request, "
            f"{moved - lookup['end']:.0f} ms to the URL; the line {line[:100]!r}"
        )
    time.sleep(1.0)
    page.cdp.screenshot(out / "map-found.png")
    results.append(f"map, from Enter to the map there: {summary(totals)}")
    results.append(
        f"map, of which: to the request {summary(before_request)}; the request "
        f"{summary(requests)}; from the answer to the URL {summary(after_answer)}"
    )

    time.sleep(TYPING_S)
    before = centre(page)
    type_into(page, MAP_FIELD, NOWHERE)
    press_enter(page)
    line = wait_for(
        page,
        "the line of an address that matched nothing",
        lambda: next(
            (s for s in status_lines(page) if s.startswith("Nenhum lugar")), None
        ),
    )
    time.sleep(0.5)
    page.cdp.screenshot(out / "map-nothing.png")
    results.append(
        f"map, nothing found: {line!r}; the map "
        f"{'stayed' if centre(page) == before else 'MOVED'}"
    )

    since = len(page.cdp.events)
    text, expected = TYPED_COORDINATES
    type_into(page, MAP_FIELD, text)
    press_enter(page)
    wait_for(
        page,
        "the map at the typed coordinates",
        lambda: (at := centre(page)) is not None and metres(at, expected) < 1_000,
    )
    page.cdp.pump(1.0)
    results.append(
        "map, coordinates typed in the address field: the map moved, with "
        f"{geocode_requests(page, since)} request(s) to /api/v1/geocode"
    )

    page.goto("/")
    wait_for(page, "the home screen", lambda: "Filtros" in page.cdp.text())
    page.cdp.click("button", "Filtros")
    wait_for(
        page, "'Perto de' with the address field", lambda: page.cdp.rect(NEAR_FIELD)
    )
    address, expected = NEAR_FILTER
    time.sleep(TYPING_S)
    type_into(page, NEAR_FIELD, address)
    page.cdp.screenshot(out / "near-typed.png")
    began = time.perf_counter()
    page.cdp.click("button", "Buscar este endereço")
    found = wait_for(
        page, "the near filter in the URL", lambda: NEAR.search(page.cdp.path())
    )
    near_ms = elapsed_ms(began)
    point = (float(found[1]), float(found[2]))
    time.sleep(0.5)
    page.cdp.screenshot(out / "near-applied.png")
    results.append(
        f"'Perto de': {address!r} became perto={point[0]},{point[1]}, "
        f"{metres(point, expected) / 1000:.1f} km from the centre of Criciúma, "
        f"in {near_ms:.0f} ms"
    )
    return results


def with_page(
    work: Path, name: str, body: Callable[[Page, Path], list[str]]
) -> list[str]:
    """A launcher of the work directory's own, the page in a headless Edge, the
    body; and both stopped, however the body ends."""
    if not (FRONTEND / "dist" / "index.html").is_file():
        raise CheckFailedError("no build: run `npm run build` in frontend/ first")
    if not EDGE.is_file():
        raise CheckFailedError(f"no Edge at {EDGE}")
    local = work / "Local"
    out = work / "out"
    for directory in (local, out):
        directory.mkdir(parents=True, exist_ok=True)
    with (out / f"{name}.launcher.stderr.txt").open("wb") as stderr:
        launcher = Launcher(local, stderr)
        page: Page | None = None
        try:
            page = Page(launcher.ready(), work / f"edge-{name}")
            return body(page, out)
        finally:
            if page is not None:
                page.close()
            launcher.stop()


def check_page(work: Path) -> list[str]:
    results = with_page(work, "page", drive)
    logs = work / "Local" / "SolidVision-dev" / "logs"
    out = work / "out"

    typed = [address for address, _ in ON_THE_MAP] + [NOWHERE, NEAR_FILTER[0]]
    words = sorted({word for text in typed for word in re.findall(r"\w{5,}", text)})
    leaks: list[str] = []
    requested = 0
    for file in sorted(logs.glob("*.log*")):
        content = file.read_text(encoding="utf-8", errors="replace")
        requested += content.count("address search requested")
        leaks += [f"{file.name}: {word}" for word in words if word in content]
    results.append(
        f"logs of the work directory: {requested} lines 'address search requested'; "
        f"of the words typed ({', '.join(words)}), found in a log: "
        f"{', '.join(leaks) or 'none'}"
    )
    results.append(f"screenshots: {out}")
    return results


# --------------------------------------------------------------------------
# The wait in the browser
# --------------------------------------------------------------------------

OSM_KEY = "solidvision.mapa.fundo-osm"
COORDINATES_FIELD = 'input[aria-label="Ir para as coordenadas"]'
MOVES = ["-27.5954, -48.5480", "-26.3045, -48.8487", "-28.6775, -49.3697"] * 2
FETCH_TIMING = (
    "(async () => { const r = await fetch('/api/v1/capabilities'); await r.json();"
    " const e = performance.getEntriesByType('resource')"
    ".filter(e => e.name.includes('/api/v1/capabilities')).pop();"
    " return {stalled: e.requestStart - e.startTime,"
    " server: e.responseStart - e.requestStart}; })()"
)


def fetch_timing(page: Page) -> dict[str, float]:
    """One request to the API from the page, as the browser timed it: how long
    it waited before it was sent, and how long the server took."""
    timing = page.cdp.evaluate(FETCH_TIMING)
    return {key: float(value) for key, value in dict(timing).items()}


def probe_stall(page: Page, out: Path) -> list[str]:
    """Where the time of a request to the API goes on the map: `/capabilities`,
    which costs the server nothing, as the screen opens and right after a move,
    with the coordinate field -- no network -- moving the map."""
    page.goto("/")
    wait_for(page, "the home screen", lambda: "Filtros" in page.cdp.text())
    time.sleep(2.0)
    home = []
    for _ in range(3):
        home.append(fetch_timing(page)["stalled"])
        time.sleep(0.5)
    results = [f"home screen: stalled {summary(home)}"]
    for osm in ("on", "off"):
        page.goto("/")
        page.cdp.evaluate(f"localStorage.setItem({json.dumps(OSM_KEY)}, '{osm}')")
        page.goto("/mapa")
        wait_for(page, "the map", lambda: page.cdp.rect(COORDINATES_FIELD))
        opened = fetch_timing(page)
        moved = []
        for place in MOVES:
            time.sleep(1.5)
            type_into(page, COORDINATES_FIELD, place)
            press_enter(page)
            moved.append(fetch_timing(page))
        results.append(
            f"map, OSM background {osm}: as the screen opens, stalled "
            f"{opened['stalled']:.0f} ms and server {opened['server']:.0f} ms; "
            f"right after a move, stalled {summary([t['stalled'] for t in moved])}, "
            f"server {summary([t['server'] for t in moved])}"
        )
    return results


def check_stall(work: Path) -> list[str]:
    return with_page(work, "stall", probe_stall)


CHECKS: dict[str, Callable[[Path], list[str]]] = {
    "service": check_service,
    "page": check_page,
    "stall": check_stall,
}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", default="")
    parser.add_argument("--work", type=Path)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    chosen = [name for name in args.only.split(",") if name] or list(CHECKS)
    work = args.work or Path(tempfile.mkdtemp(prefix="rfc043-address-"))
    heading("The machine")
    describe_machine()
    say(f"work directory {work}")
    outcome: dict[str, list[str]] = {}
    failed = False
    try:
        for name in chosen:
            heading(name)
            try:
                outcome[name] = CHECKS[name](work)
            except CheckFailedError as error:
                failed = True
                outcome[name] = [f"FAILED: {error}"]
            except Exception:  # noqa: BLE001
                failed = True
                outcome[name] = [f"ERROR: {traceback.format_exc()}"]
    finally:
        heading("Results")
        for name, lines in outcome.items():
            for line in lines:
                say(f"{name}: {line}")
        if not args.keep and args.work is None:
            shutil.rmtree(work, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
