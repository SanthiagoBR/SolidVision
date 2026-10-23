r"""The listing by date, measured: the server, the page and the checks (RFC-044 §9).

    python measure_browse.py --keep > measure_browse.log 2>&1

    --only a,b      just these, in the order of CHECKS; `prepare` always runs
    --work PATH     the work directory (default: %USERPROFILE%\SolidVision-medicoes\
                    rfc-044 -- outside %TEMP%, whose empty folders something
                    sweeps, which broke RFC-040's cluster: RFC-040 section 14.1)
    --keep          keep it; without it, it is removed at the end
    --dji PATH      the folder of real DJI photos, with GPS (default:
                    %USERPROFILE%\Desktop\demo, RFC-032's 40)

**The database** is the work directory's own, never the user's: RFC-022's
dataset and the 40 DJI photos, indexed through the API by a launcher of this
work directory (`LOCALAPPDATA` under it, `DATABASE_SOURCE=embedded`,
`WARM_UP_MODELS=true`); then 100 000 rows written by RFC-040's
`measure_map.py --seed-rows`, inside `embedded run`: 2 000 places with Zipf
weights (s = 0.85), 60 m of scatter, dates from 2015 to 2026 and 5% with none,
on a device that is not connected. The busiest place is the plan's first.

**The page is measured in a headless Edge** over the launcher's API, as RFC-043
section 9.2 did, and not in the Electron window: the window's single-instance
lock would give way to one the person running this has open. A stand-in for the
window's bridge is put on the page before it loads, and keeps the `search` marks
the page reports (`reportSearch`, RFC-038 section 5.6) -- the same code that
writes them to `shell.log` in the window. **Nothing opens on the desktop.**

**The server's numbers** come from `EXPLAIN (ANALYZE, TIMING OFF, SUMMARY ON)`
of the exact statements the repository sends (`listing_statement()`,
`summary_statement()`), run inside `embedded run` with the launcher stopped:
planning and execution, the median of five; and the plan says how the rows are
sorted. The round trip is the API's, over HTTP, with the launcher up and the
search's models loaded.

No new dependency: RFC-040's `measure_map.py` gives the debugging-protocol
client, the seed and the helpers.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import statistics
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO, Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BACKEND = REPO / "backend"
FRONTEND = REPO / "frontend"
MAP_SCRIPT = REPO / "experiments" / "rfc-040-map" / "measure_map.py"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(MAP_SCRIPT.parent))

from measure_map import (  # noqa: E402
    BENCH_DEVICE,
    CREATE_NO_WINDOW,
    SEED_ROWS,
    Cdp,
    CheckFailedError,
    heading,
    http,
    ms,
    say,
    seed_plan,
    wait_until,
)

PYTHON = BACKEND / ".venv" / "Scripts" / "python.exe"
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
EDGE_PORT = 9344
READY_SECONDS = 300.0
STOP_SECONDS = 90.0
JOB_SECONDS = 1800.0
RUNS = 5
RADIUS_M = 1000
DJI_CELL = (-26.32, -48.82)
DJI_ZOOM = 12  # cells of ~1 km (`precisionForZoom`: 2 from zoom 11 to 13)
BROWSE_TEXT = "Ver todas as fotos daqui, por data"
LIST_CHANGED = "A lista mudou desde a última página."

# The window's bridge, as far as the page uses it: the marks of the searches are
# kept for the script to read.
BRIDGE = """
window.__marks = [];
window.solidvision = {
  onLauncherEvent: () => () => {},
  openLogsFolder: () => {},
  reportHome: () => {},
  reportSearch: (report) => { window.__marks.push(report); },
};
"""
CARDS = "[...document.querySelectorAll('ol[aria-label=Resultados] > li[data-id]')]"


# --------------------------------------------------------------------------
# The launcher and the page
# --------------------------------------------------------------------------


def environment(local: Path, **extra: str) -> dict[str, str]:
    """RFC-040's: the work directory's data root and the embedded cluster."""
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
        WARM_UP_MODELS="true",
        PYTHONPATH=str(BACKEND),
    )
    kept.update(extra)
    return kept


class Launcher:
    """`python -m app.launcher --supervised`, until its stdin ends."""

    def __init__(self, local: Path, stderr: IO[bytes], **extra: str) -> None:
        self.process = subprocess.Popen(
            [str(PYTHON), "-m", "app.launcher", "--supervised"],
            cwd=REPO,
            env=environment(local, **extra),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            creationflags=CREATE_NO_WINDOW,
        )
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.url = self._ready()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                self.events.put(json.loads(line))
            except ValueError:
                continue

    def _ready(self) -> str:
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
            if event.get("event") == "ready":
                return str(event["url"]).rstrip("/")
            if event.get("event") in ("failed", "already_running"):
                raise CheckFailedError(f"the launcher said {event}")
        raise CheckFailedError(f"no ready in {READY_SECONDS:.0f} s")

    def search_ready(self) -> None:
        """The models loaded: rounds timed while they load measure the loading
        (RFC-040 section 14.1)."""

        def answered() -> bool:
            _, body = http(self.url, "/health")
            return isinstance(body, dict) and body.get("search") == "ready"

        found, _ = wait_until(answered, READY_SECONDS, 0.5)
        if not found:
            raise CheckFailedError("/health never said search: ready")

    def stop(self) -> None:
        if self.process.poll() is None:
            assert self.process.stdin is not None
            self.process.stdin.close()
            try:
                self.process.wait(STOP_SECONDS)
            except subprocess.TimeoutExpired:
                say(f"the launcher did not stop in {STOP_SECONDS:.0f} s; killed")
                self.process.kill()


class Page:
    """The interface in a headless Edge, with the bridge's stand-in."""

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
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def target() -> str | None:
            status, targets = http(f"http://127.0.0.1:{EDGE_PORT}", "/json/list")
            if status != 200:
                return None
            for each in targets:
                if each.get("type") == "page":
                    return str(each["webSocketDebuggerUrl"])
            return None

        ws_url, _ = wait_until(target, 60)
        if ws_url is None:
            raise CheckFailedError("no page target in the headless Edge")
        self.cdp = Cdp(ws_url)
        self.cdp.call("Page.enable")
        self.cdp.call("Page.addScriptToEvaluateOnNewDocument", source=BRIDGE)

    def goto(self, path: str) -> None:
        """The page at this path, loaded anew: an empty cache, as a window that
        just opened has."""
        self.cdp.call("Page.navigate", url=self.url + path)
        wait_until(
            lambda: self.cdp.evaluate("document.readyState") == "complete", 30, 0.1
        )

    def wait(self, what: str, predicate: Callable[[], Any], seconds: float = 60) -> Any:
        value, _ = wait_until(predicate, seconds, 0.02)
        if not value:
            raise CheckFailedError(
                f"{what}, not in {seconds:.0f} s; the screen says "
                f"{self.cdp.text()[:300]!r}"
            )
        return value

    def cards(self) -> list[str]:
        return list(self.cdp.evaluate(f"{CARDS}.map(e => e.dataset.id)") or [])

    def marks(self) -> list[dict[str, Any]]:
        return list(
            self.cdp.evaluate("window.__marks.filter(m => m.kind === 'browse')") or []
        )

    def next_mark(self, before: int, seconds: float = 120) -> dict[str, Any]:
        def arrived() -> dict[str, Any] | None:
            marks = self.marks()
            return marks[before] if len(marks) > before else None

        return dict(self.wait("the page's mark", arrived, seconds))

    def params(self) -> dict[str, list[str]]:
        search = str(self.cdp.evaluate("location.search") or "")
        return urllib.parse.parse_qs(search.lstrip("?"), keep_blank_values=True)

    def close(self) -> None:
        self.cdp.close()
        self.process.kill()
        self.process.wait(30)


# --------------------------------------------------------------------------
# The measurement
# --------------------------------------------------------------------------


class Measurement:
    def __init__(self, work: Path, dji: Path) -> None:
        self.work = work.resolve()
        self.dji = dji
        self.local = self.work / "Local"
        self.logs = self.local / "SolidVision-dev" / "logs"
        self.out = self.work / "out"
        for directory in (self.local, self.out):
            directory.mkdir(parents=True, exist_ok=True)
        self.launcher: Launcher | None = None
        self.page: Page | None = None
        self.stderr: IO[bytes] | None = None
        self.circles: set[tuple[float, float]] = {DJI_CELL}
        places, _ = seed_plan(1)
        self.busiest = (round(places[0][0], 6), round(places[0][1], 6))

    # The launcher and the page, started on first use and stopped together.

    def api(self) -> str:
        if self.launcher is None:
            self.stderr = (self.out / "launcher.stderr.txt").open("ab")
            self.launcher = Launcher(self.local, self.stderr)
            self.launcher.search_ready()
        return self.launcher.url

    def browser(self) -> Page:
        url = self.api()
        if self.page is None:
            self.page = Page(url, self.work / "edge")
        return self.page

    def stop(self) -> None:
        if self.page is not None:
            self.page.close()
            self.page = None
        if self.launcher is not None:
            self.launcher.stop()
            self.launcher = None
        if self.stderr is not None:
            self.stderr.close()
            self.stderr = None

    def browse(self, circle: tuple[float, float], **query: Any) -> dict[str, Any]:
        self.circles.add(circle)
        params = {"near_lat": circle[0], "near_lon": circle[1], "radius_m": RADIUS_M}
        params.update(query)
        status, body = http(
            self.api(),
            "/api/v1/images/browse?" + urllib.parse.urlencode(params, doseq=True),
        )
        if status != 200:
            raise CheckFailedError(f"GET /images/browse {status}: {body}")
        return dict(body)

    def walk(
        self, circle: tuple[float, float], order: str, **query: Any
    ) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
        """Every page, until `next_after` is null: the photos, the summary, and
        how many pages."""
        first = self.browse(circle, order=order, limit=40, **query)
        photos = list(first["results"])
        after, pages = first["next_after"], 1
        while after is not None:
            page = self.browse(circle, order=order, limit=100, after=after, **query)
            photos += page["results"]
            after, pages = page["next_after"], pages + 1
        return photos, first["summary"], pages

    def listing_path(self, circle: tuple[float, float], extra: str = "") -> str:
        self.circles.add(circle)
        return f"/busca?todas&perto={circle[0]},{circle[1]}&raio={RADIUS_M}{extra}"

    def embedded_run(self, *arguments: str) -> str:
        """This script, inside `embedded run` of the work directory's cluster."""
        self.stop()
        done = subprocess.run(
            [
                str(PYTHON),
                "-m",
                "app.infrastructure.database.embedded",
                "run",
                "--",
                str(PYTHON),
                *arguments,
            ],
            cwd=BACKEND,
            env=environment(self.local),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3600,
            creationflags=CREATE_NO_WINDOW,
        )
        if done.returncode != 0:
            raise CheckFailedError(f"embedded run failed: {done.stderr[-800:]}")
        return done.stdout


def count_indexed(url: str) -> int:
    return sum(int(device["indexed_images"]) for device in devices(url))


def devices(url: str) -> list[dict[str, Any]]:
    status, body = http(url, "/api/v1/devices")
    if status != 200:
        raise CheckFailedError(f"GET /devices: {status} {body}")
    return list(body["devices"])


def index(url: str, folders: Sequence[Path], label: str) -> int:
    """Register the volume that holds the folders and index them, through the
    API -- RFC-040's `Measurement.index()`."""
    status, listed = http(url, "/api/v1/volumes")
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
        url,
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
        url, "/api/v1/jobs", "POST", {"device_id": device["id"], "scopes": scopes}
    )
    if status >= 300:
        raise CheckFailedError(f"POST /jobs: {status} {job}")

    def done() -> dict[str, Any] | None:
        _, now = http(url, f"/api/v1/jobs/{job['id']}")
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
    say(f"indexed {finished['processed_images']} images in {took:.1f} s")
    return int(finished["processed_images"])


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------


def check_prepare(m: Measurement) -> str:
    """RFC-022's dataset and the 40 DJI photos, indexed, once."""
    url = m.api()
    indexed = count_indexed(url)
    if indexed == 0:
        from dataset_tools.manifest import (
            DEMO_CORPUS_ROOT,
            DEMO_MANIFEST_PATH,
            load_manifest,
        )
        from dataset_tools.materialize import materialize

        corpus = m.work / "corpora" / "demo"
        if not corpus.exists():
            materialize(load_manifest(DEMO_MANIFEST_PATH), DEMO_CORPUS_ROOT, corpus)
        index(url, [corpus], "DATASET")
        index(url, [m.dji], "DJI")
        indexed = count_indexed(url)
    return f"{indexed} photos indexed, the API at {url}"


def check_dji(m: Measurement) -> str:
    """Criterion 3: from the click in the balloon of the DJI cell to the grid."""
    page = m.browser()
    painted: list[float] = []
    responded: list[float] = []
    counts: set[int] = set()
    seen: dict[str, list[str]] = {}
    for _ in range(RUNS):
        page.goto(f"/mapa?centro={DJI_CELL[0]},{DJI_CELL[1]}&zoom={DJI_ZOOM}")
        page.wait(
            "the list button",
            lambda: page.cdp.evaluate(
                "[...document.querySelectorAll('button')]"
                ".some(b => b.textContent.trim() === 'Lista')"
            ),
        )
        page.cdp.click("button", "Lista")
        page.wait(
            "the row of 10 photos",
            lambda: page.cdp.evaluate(
                "[...document.querySelectorAll('aside li button')]"
                ".some(b => b.textContent.startsWith('10 fotos'))"
            ),
        )
        page.cdp.click("aside li button", "10 fotos")
        page.wait(
            "the balloon's button",
            lambda: page.cdp.evaluate(
                "[...document.querySelectorAll('button')].some(b => "
                f"b.textContent.trim() === {json.dumps(BROWSE_TEXT)} && !b.disabled)"
            ),
        )
        before = len(page.marks())
        page.cdp.click("button", BROWSE_TEXT)
        mark = page.next_mark(before)
        painted.append(float(mark["paintedMs"]))
        responded.append(float(mark["responseMs"]))
        counts.add(int(mark["results"]))
        seen = page.params()
    cards = page.cards()
    return (
        f"painted {ms(painted)}; the response {ms(responded)}; {sorted(counts)} "
        f"photos; the listing at todas={seen.get('todas')} perto={seen.get('perto')} "
        f"raio={seen.get('raio')} q={seen.get('q')}; {len(cards)} cards"
    )


def check_seed(m: Measurement) -> str:
    """The 100 000, by RFC-040's seed, unless they are there already."""
    url = m.api()
    bench = [d for d in devices(url) if d["id"] == str(BENCH_DEVICE)]
    if bench and bench[0]["indexed_images"] >= SEED_ROWS:
        return f"{bench[0]['indexed_images']} rows of HD-BENCH already there"
    began = time.perf_counter()
    printed = m.embedded_run(str(MAP_SCRIPT), "--seed-rows", str(SEED_ROWS))
    first = m.browse(m.busiest, limit=1)
    return (
        f"{printed.strip()} ({time.perf_counter() - began:.0f} s with the cluster "
        f"started and stopped); the busiest place at {m.busiest}: "
        f"{first['summary']['total']} photos in {RADIUS_M} m, "
        f"{first['summary']['undated']} undated"
    )


def check_server(m: Measurement) -> str:
    """Criterion 2 and the index: EXPLAIN inside the cluster, and the HTTP round
    trip."""
    printed = m.embedded_run(
        str(Path(__file__).resolve()),
        "--explain",
        str(m.busiest[0]),
        str(m.busiest[1]),
    )
    rows = [json.loads(line) for line in printed.splitlines() if line.startswith("{")]
    for row in rows:
        say(
            f"{row['case']}: planning {ms(row['planning'])}; execution "
            f"{ms(row['execution'])}; sort {row['sort']}; rows {row['rows']}"
        )
    # The round trip, with the launcher up and the models loaded.
    first_ms: list[float] = []
    middle_ms: list[float] = []
    summary = m.browse(m.busiest, limit=1)["summary"]
    total = int(summary["total"])
    # The cursor of the middle: the last photo of the first half.
    photos, _, _ = m.walk(m.busiest, "newest")
    middle = photos[total // 2]["id"]
    for _ in range(RUNS):
        began = time.perf_counter()
        m.browse(m.busiest, limit=40)
        first_ms.append((time.perf_counter() - began) * 1000)
        began = time.perf_counter()
        m.browse(m.busiest, limit=100, after=middle)
        middle_ms.append((time.perf_counter() - began) * 1000)
    say(f"round trip, first page (40) with the summary: {ms(first_ms)}")
    say(f"round trip, a page of 100 from the middle by the cursor: {ms(middle_ms)}")
    by_case = {row["case"]: row for row in rows}
    circle = statistics.median(by_case["circle-first"]["execution"])
    for variant in ("captured_at", "full-key"):
        indexed = by_case.get(f"circle-first+{variant}")
        if indexed is not None:
            ratio = statistics.median(indexed["execution"]) / circle
            say(f"the index on {variant}: the circle's first page at {ratio:.2f}x")
    return (
        f"{total} photos in the circle; EXPLAIN first page "
        f"{statistics.median(by_case['circle-first']['execution']):.1f} ms + "
        f"summary {statistics.median(by_case['circle-summary']['execution']):.1f} ms, "
        f"middle page {statistics.median(by_case['circle-middle']['execution']):.1f} "
        f"ms (sort: {by_case['circle-first']['sort']}); round trip "
        f"{statistics.median(first_ms):.0f} ms and "
        f"{statistics.median(middle_ms):.0f} ms"
    )


def check_order(m: Measurement) -> str:
    """Criterion 1: the grid's ids are the responses', three pages, both ways."""
    page = m.browser()
    said: list[str] = []
    for order, extra in (("newest", ""), ("oldest", "&ordem=antigas")):
        page.goto(m.listing_path(m.busiest, extra))
        page.wait("the first page", lambda: len(page.cards()) == 40)
        for shown in (140, 240):
            page.cdp.click("button", "Mostrar mais")
            page.wait(f"{shown} cards", lambda s=shown: len(page.cards()) == s)
        on_screen = page.cards()
        first = m.browse(m.busiest, order=order, limit=40)
        second = m.browse(m.busiest, order=order, limit=100, after=first["next_after"])
        third = m.browse(m.busiest, order=order, limit=100, after=second["next_after"])
        answered = [
            r["id"] for r in first["results"] + second["results"] + third["results"]
        ]
        if on_screen != answered:
            raise CheckFailedError(f"{order}: the grid is not the responses' order")
        away = page.cdp.evaluate(
            f"{CARDS}.filter(e => e.textContent.includes('HD-BENCH · desconectado'))"
            ".length"
        )
        said.append(f"{order}: 240 ids equal; {away} cards say HD-BENCH · desconectado")
    return "; ".join(said)


def check_more(m: Measurement) -> str:
    """Criterion 4: "Mostrar mais", from the click to the 100 new painted."""
    page = m.browser()
    page.goto(m.listing_path(m.busiest))
    page.wait("the first page", lambda: len(page.cards()) == 40)
    painted: list[float] = []
    responded: list[float] = []
    for round_ in range(RUNS):
        before = len(page.marks())
        page.wait(
            "the button",
            lambda: page.cdp.evaluate(
                "[...document.querySelectorAll('button')]"
                ".some(b => b.textContent.trim() === 'Mostrar mais' && !b.disabled)"
            ),
        )
        page.cdp.click("button", "Mostrar mais")
        mark = page.next_mark(before)
        if int(mark["results"]) != 100 or int(mark["limit"]) != 100:
            raise CheckFailedError(f"the mark of a page of 100 was {mark}")
        painted.append(float(mark["paintedMs"]))
        responded.append(float(mark["responseMs"]))
        page.wait("the cards", lambda r=round_: len(page.cards()) == 40 + 100 * (r + 1))
    progress = page.cdp.evaluate(
        "[...document.querySelectorAll('span')].map(s => s.textContent)"
        ".find(t => / de [\\d.]+$/.test(t))"
    )
    return (
        f"painted {ms(painted)}; the response {ms(responded)}; "
        f"{len(page.cards())} cards at the end, beside the button {progress!r}"
    )


def check_whole(m: Measurement) -> str:
    """The whole list through the API, both ways."""
    newest, summary, pages = m.walk(m.busiest, "newest")
    oldest, _, _ = m.walk(m.busiest, "oldest")
    ids = [photo["id"] for photo in newest]
    problems = []
    if len(ids) != summary["total"] or len(set(ids)) != len(ids):
        problems.append(
            f"{len(ids)} seen, {len(set(ids))} distinct, total {summary['total']}"
        )
    if set(ids) != {photo["id"] for photo in oldest}:
        problems.append("the two directions are not the same photos")
    dates_new = [p["captured_at"] for p in newest if p["captured_at"]]
    dates_old = [p["captured_at"] for p in oldest if p["captured_at"]]
    if dates_new != list(reversed(dates_old)):
        problems.append("the dated part of one way is not the other's reversed")
    for name, photos in (("newest", newest), ("oldest", oldest)):
        undated = [i for i, p in enumerate(photos) if p["captured_at"] is None]
        if undated and undated[0] != len(photos) - len(undated):
            problems.append(f"{name}: an undated photo before a dated one")
    ties = len(dates_new) - len(set(dates_new))
    if problems:
        raise CheckFailedError("; ".join(problems))
    return (
        f"{len(ids)} photos in {pages} pages each way = the summary's total; none "
        f"repeated; the dates of one way are the other's reversed ({ties} photos "
        f"share a date); {summary['undated']} undated close both"
    )


def check_doors(m: Measurement) -> str:
    """The three doors, and the balloon of a map that carries a search."""
    page = m.browser()
    said = []
    lat, lon = DJI_CELL
    doors = {
        "the home screen": f"/?perto={lat},{lon}&raio={RADIUS_M}",
        "a search": f"/busca?q=telhado&perto={lat},{lon}&raio={RADIUS_M}",
    }
    for name, path in doors.items():
        page.goto(path)
        page.wait(
            "the action",
            lambda: page.cdp.evaluate(
                "[...document.querySelectorAll('button')]"
                f".some(b => b.textContent.trim() === {json.dumps(BROWSE_TEXT)})"
            ),
        )
        page.cdp.click("button", BROWSE_TEXT)
        page.wait("the listing", lambda: "todas" in page.params())
        found = page.params()
        said.append(
            f"{name}: perto={found.get('perto')} raio={found.get('raio')} "
            f"q={found.get('q')}"
        )
    page.goto(f"/mapa?centro={lat},{lon}&zoom={DJI_ZOOM}&q=telhado")
    page.wait(
        "the list button",
        lambda: page.cdp.evaluate(
            "[...document.querySelectorAll('button')]"
            ".some(b => b.textContent.trim() === 'Lista')"
        ),
    )
    page.cdp.click("button", "Lista")
    page.wait(
        "the row",
        lambda: page.cdp.evaluate(
            "[...document.querySelectorAll('aside li button')]"
            ".some(b => b.textContent.startsWith('10 fotos'))"
        ),
    )
    page.cdp.click("aside li button", "10 fotos")
    page.wait(
        "the balloon",
        lambda: page.cdp.evaluate(
            "[...document.querySelectorAll('button')].some(b => "
            f"b.textContent.trim() === {json.dumps(BROWSE_TEXT)} && !b.disabled)"
        ),
    )
    balloon = page.cdp.evaluate(
        "[...document.querySelectorAll('.leaflet-popup button')]"
        ".map(b => b.textContent.trim())"
    )
    page.cdp.click("button", BROWSE_TEXT)
    page.wait("the listing", lambda: "todas" in page.params())
    found = page.params()
    said.append(
        f"the balloon of a map carrying “telhado” ({balloon}): perto="
        f"{found.get('perto')} raio={found.get('raio')} q={found.get('q')}"
    )
    return "; ".join(said)


def check_notices(m: Measurement) -> str:
    """A date range over the 100 000: the API's numbers on the screen, and the
    undated back at the end once the range is taken off."""
    page = m.browser()
    api = m.browse(
        m.busiest, limit=1, captured_from="2018-01-01", captured_to="2021-01-01"
    )["summary"]
    page.goto(m.listing_path(m.busiest, "&de=2018-01-01&ate=2020-12-31"))
    page.wait("the first page", lambda: len(page.cards()) == 40)
    notice = page.wait(
        "the notice",
        lambda: page.cdp.evaluate(
            "[...document.querySelectorAll('p')].map(p => p.textContent)"
            ".find(t => t.includes('data de captura.'))"
        ),
    )
    expected = f"{api['excluded_unknown_date']:,}".replace(",", ".")
    if not str(notice).startswith(expected):
        raise CheckFailedError(f"the notice says {notice!r}, the API {expected}")
    header = page.cdp.evaluate(
        "document.querySelector('[role=status]')?.textContent ?? ''"
    )
    page.cdp.click("button", "Tirar o filtro de datas")
    page.wait(
        "no range", lambda: "de" not in page.params() and "todas" in page.params()
    )
    page.wait("the first page", lambda: len(page.cards()) == 40)
    after = page.wait(
        "the header",
        lambda: page.cdp.evaluate(
            "[...document.querySelectorAll('[role=status]')].map(e => e.textContent)"
            ".find(t => t.includes('sem data'))"
        ),
    )
    whole = m.browse(m.busiest, limit=1)["summary"]
    tail = m.browse(m.busiest, order="oldest", limit=1)
    return (
        f"under 2018-2020: {notice!r}, the API's {api['excluded_unknown_date']}; the "
        f"header {header!r}; without the range: {after!r} (the API: "
        f"{whole['undated']} undated), the list ending undated: "
        f"{tail['summary']['undated'] > 0}"
    )


def check_nomodel(m: Measurement) -> str:
    """No model: the bar disabled with the search_detail, the listing answering."""
    m.stop()
    empty = m.work / "no-models"
    empty.mkdir(exist_ok=True)
    with (m.out / "nomodel.stderr.txt").open("wb") as stderr:
        launcher = Launcher(m.local, stderr, MODEL_RUNTIME_DIRECTORY=str(empty))
        page: Page | None = None
        try:
            _, health = http(launcher.url, "/health")
            page = Page(launcher.url, m.work / "edge-nomodel")
            page.goto(m.listing_path(DJI_CELL))
            page.wait("the listing", lambda: len(page.cards()) == 10)
            bar = page.cdp.evaluate(
                "(() => { const i = document.querySelector('input[type=search]');"
                " return i ? {disabled: i.disabled, placeholder: i.placeholder} : null;"
                " })()"
            )
            return (
                f"/health search={health.get('search')!r}; the bar {bar}; the "
                f"listing with {len(page.cards())} photos; the detail "
                f"{str(health.get('search_detail'))[:90]!r}"
            )
        finally:
            if page is not None:
                page.close()
            launcher.stop()


def check_logs(m: Measurement) -> str:
    """No coordinate of any circle used, in any log of the work directory."""
    m.stop()
    texts = set()
    for lat, lon in m.circles:
        texts |= {str(lat), str(lon), f"{lat:.4f}", f"{lon:.4f}"}
    found = []
    lines = 0
    browse_lines = 0
    for file in sorted(m.logs.glob("*.log*")):
        content = file.read_text(encoding="utf-8", errors="replace")
        lines += content.count("\n")
        browse_lines += content.count("browse completed")
        found += [f"{file.name}: {text}" for text in texts if text in content]
    if found:
        raise CheckFailedError(f"found {found}")
    return (
        f"none of {len(texts)} texts of {len(m.circles)} circles in {lines} lines of "
        f"{', '.join(f.name for f in sorted(m.logs.glob('*.log*')))}; "
        f"{browse_lines} lines 'browse completed'"
    )


CHECKS: dict[str, Callable[[Measurement], str]] = {
    "prepare": check_prepare,
    "dji": check_dji,
    "seed": check_seed,
    "server": check_server,
    "order": check_order,
    "more": check_more,
    "whole": check_whole,
    "doors": check_doors,
    "notices": check_notices,
    "nomodel": check_nomodel,
    "logs": check_logs,
}


# --------------------------------------------------------------------------
# Inside `embedded run`: the EXPLAINs (`--explain LAT LON`)
# --------------------------------------------------------------------------


def explain_rounds(latitude: float, longitude: float) -> None:
    """The statements of the repository, EXPLAINed five times each; one JSON line
    per case."""
    from sqlalchemy import text

    from app.domain.value_objects.device_id import DeviceId
    from app.domain.value_objects.geo_circle import GeoCircle
    from app.domain.value_objects.listing import ListingOrder
    from app.domain.value_objects.position import Position
    from app.domain.value_objects.search_filters import SearchFilters
    from app.infrastructure.persistence.postgres_image_repository import (
        PostgresImageRepository,
    )
    from app.infrastructure.persistence.session import get_db

    session = next(get_db())
    repository = PostgresImageRepository(session)
    circle = SearchFilters(
        taken_within=GeoCircle(Position(latitude, longitude), RADIUS_M)
    )
    device = SearchFilters(device_ids=frozenset({DeviceId(BENCH_DEVICE)}))
    total = repository.summarize_listing(circle).total
    middle = repository.list_by_filters(circle, ListingOrder.NEWEST, total // 2, None)[
        -1
    ]
    cases = {
        "circle-first": PostgresImageRepository.listing_statement(
            circle, ListingOrder.NEWEST, 41
        ),
        "circle-summary": PostgresImageRepository.summary_statement(circle),
        "circle-middle": PostgresImageRepository.listing_statement(
            circle, ListingOrder.NEWEST, 101, middle
        ),
        "device-first": PostgresImageRepository.listing_statement(
            device, ListingOrder.NEWEST, 41
        ),
    }

    def explain(statement: Any) -> tuple[float, float, str, int]:
        # `render_postcompile`: the device set is an expanding `IN`, which the
        # driver would otherwise receive as a placeholder it cannot fill.
        compiled = statement.compile(
            dialect=session.get_bind().dialect,
            compile_kwargs={"render_postcompile": True},
        )
        plan = (
            session.connection()
            .exec_driver_sql(
                "EXPLAIN (ANALYZE, TIMING OFF, SUMMARY ON, FORMAT JSON) "
                + str(compiled),
                dict(compiled.params),
            )
            .scalar_one()
        )[0]
        return (
            float(plan["Planning Time"]),
            float(plan["Execution Time"]),
            sort_of(plan["Plan"]),
            int(plan["Plan"].get("Actual Rows", 0)),
        )

    def measure(name: str, statement: Any) -> None:
        planning, execution = [], []
        sort, rows = "", 0
        explain(statement)  # one warm-up, not counted
        for _ in range(RUNS):
            p, e, sort, rows = explain(statement)
            planning.append(p)
            execution.append(e)
        print(
            json.dumps(
                {
                    "case": name,
                    "planning": planning,
                    "execution": execution,
                    "sort": sort,
                    "rows": rows,
                }
            ),
            flush=True,
        )

    for name, statement in cases.items():
        measure(name, statement)
    indexes = {
        "captured_at": "CREATE INDEX ix_bench_captured_at ON images (captured_at)",
        "full-key": (
            "CREATE INDEX ix_bench_listing ON images "
            '(captured_at DESC NULLS LAST, relative_path COLLATE "C", id)'
        ),
    }
    for variant, ddl in indexes.items():
        session.execute(text(ddl))
        session.execute(text("ANALYZE images"))
        for name in ("circle-first", "device-first"):
            measure(f"{name}+{variant}", cases[name])
        session.rollback()


def sort_of(node: dict[str, Any]) -> str:
    """How the plan sorts: the first `Sort Method` under the top node, or the
    scan that hands the rows over in order."""
    found = []

    def visit(each: dict[str, Any]) -> None:
        if "Sort Method" in each:
            found.append(f"{each['Node Type']}: {each['Sort Method']}")
        elif each.get("Node Type", "").startswith("Index"):
            found.append(f"{each['Node Type']} on {each.get('Index Name')}")
        for child in each.get("Plans", []):
            visit(child)

    visit(node)
    return ", ".join(found) or "none"


# --------------------------------------------------------------------------


def describe_machine() -> None:
    import platform

    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    say(f"{platform.platform()}, {platform.processor()}")
    say(f"Python {platform.python_version()}, at {time.strftime('%Y-%m-%dT%H:%M:%S')}")
    say(f"checkout {commit} plus the working tree")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", default="")
    parser.add_argument("--work", type=Path)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--dji", type=Path, default=Path.home() / "Desktop" / "demo")
    parser.add_argument("--explain", nargs=2, type=float, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.explain:
        explain_rounds(*args.explain)
        return 0

    if not (FRONTEND / "dist" / "index.html").is_file():
        print("no build: run `npm run build` in frontend/ first")
        return 2
    chosen = [name for name in args.only.split(",") if name] or list(CHECKS)
    if "prepare" not in chosen:
        chosen.insert(0, "prepare")
    work = args.work or Path.home() / "SolidVision-medicoes" / "rfc-044"
    heading("The machine")
    describe_machine()
    say(f"work directory {work}; the DJI folder {args.dji}")
    m = Measurement(work, args.dji)
    outcome: list[tuple[str, str]] = []
    failed = False
    try:
        for name in CHECKS:
            if name not in chosen:
                continue
            heading(name)
            began = time.perf_counter()
            try:
                result = CHECKS[name](m)
            except CheckFailedError as error:
                failed = True
                result = f"FAILED: {error}"
            except Exception:  # noqa: BLE001
                failed = True
                result = f"ERROR: {traceback.format_exc()}"
            say(f"RESULT {name}: {result} [{time.perf_counter() - began:.0f} s]")
            outcome.append((name, result))
    finally:
        m.stop()
        heading("Results")
        for name, result in outcome:
            say(f"{name}: {result}")
        if not args.keep:
            shutil.rmtree(m.work, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
