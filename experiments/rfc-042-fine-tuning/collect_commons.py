"""Collect aerial and drone photographs from Wikimedia Commons (RFC-042 section 4).

RFC-042 trains CLIP on image-text pairs whose images come, first of all, from
Wikimedia Commons (section 4.2), with a provenance row for every image
(section 4.4). This script does the slow, network-bound part once: it finds
the files, reads each one's license and author, and downloads a thumbnail of
the ones that pass the filters. Captioning, the frozen splits and the
WebDataset shards that go to the Hugging Face dataset are later, local steps
that read what it leaves behind.

    python experiments/rfc-042-fine-tuning/collect_commons.py \
        --out D:/rfc042/commons --contact you@example.com

Four phases, run in order by default (`--phase` runs one):

1. **discover** walks the categories of `commons_categories.toml` down to each
   root's depth and records every image file it meets.
2. **describe** asks the API, 50 files a request, for each file's size, MIME
   type, sha1, thumbnail URL and `extmetadata`: license, author, description,
   capture date, coordinates, categories.
3. **download** applies the filters of the TOML file to the stored metadata
   and downloads the thumbnail of every accepted file, checked before it is
   kept.
4. **export** writes `provenance.csv`, one row per downloaded image, with the
   fields of RFC-042 section 4.4 and the decision of the current filters.

`--phase status` re-applies the filters and prints what was found, rejected
and downloaded, without touching the network. It is how the TOML file is
tuned between `describe` and `download`.

**Everything resumes.** The state lives in `collection.sqlite`, in the output
directory. A phase that is interrupted (Ctrl+C, a dropped connection, a
reboot) continues where it stopped when the same command runs again, and
nothing is fetched twice. The filters are re-applied on every run to every
file not yet downloaded, so changing them costs no request.

**Wikimedia's rules for automated clients**, which this script follows
(https://www.mediawiki.org/wiki/Wikimedia_APIs/Rate_limits and
https://wikitech.wikimedia.org/wiki/Robot_policy, read on 2026-10-31):

- A User-Agent naming the client and a way to reach its operator. Without one
  a client is "unidentified" and gets 10 API requests a minute; with one, 200.
  `--contact` is that way, and is required.
- API requests in series, at most one a second, with `maxlag=5`, and five
  seconds of rest after any request that took more than one.
- Media: at most two downloads at once, at most 25 Mbps over 10 s (this script
  stops at 20 by default), thumbnails rather than originals, and only the
  standard widths. Since January 2026 Commons answers other widths with 429;
  500 px is a standard one.
- `Retry-After` is honoured on every 429 and 503.

The thumbnail URL is always the one the API returned, never one rebuilt by
hand, and a download is kept only if it decodes as an image of the size the
API announced. RFC-022 section 9.5 saved three HTTP 429 pages as JPEGs by
doing otherwise.

The thumbnails are kept as Commons served them, 500 px wide: enough detail for
a captioning model, and the packaging step reduces them for training and
strips their metadata.

Run it with the project's virtualenv (it needs Pillow), with the log next to
the data:

    python experiments/rfc-042-fine-tuning/collect_commons.py --out DIR \
        --contact EMAIL >> DIR/collect.log 2>&1
"""

from __future__ import annotations

import argparse
import csv
import datetime
import email.utils
import fnmatch
import gzip
import hashlib
import html.parser
import http.client
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat

API_URL = "https://commons.wikimedia.org/w/api.php"
CLIENT = "SolidVision-RFC042-collector/1.0"
DEFAULT_CATEGORIES = Path(__file__).with_name("commons_categories.toml")
DATABASE_NAME = "collection.sqlite"
PROVENANCE_NAME = "provenance.csv"

# The thumbnail widths Wikimedia production serves since January 2026
# (mediawiki.org, "Common thumbnail sizes"). Any other width is throttled.
STANDARD_WIDTHS = (20, 40, 60, 120, 250, 330, 500, 960, 1280, 1920, 3840)
THUMB_WIDTH = 500
assert THUMB_WIDTH in STANDARD_WIDTHS

# The most `pageids` one API request accepts from a client without bot rights.
DESCRIBE_BATCH = 50
# Robot policy, media: "a total concurrency of at most 2".
DOWNLOAD_WORKERS = 2
DEFAULT_MAX_MBPS = 20.0
MAX_THUMB_BYTES = 10 * 1024 * 1024
MAX_API_BYTES = 50 * 1024 * 1024
# A file whose download failed this many times, for transient reasons, is
# given up until the filters change.
MAX_ATTEMPTS = 5
# This many failed downloads in a row stops the run: the network, or
# Wikimedia, is saying something that retrying will not fix.
MAX_CONSECUTIVE_FAILURES = 20

# Files with other extensions (video, audio, PDF, SVG) are not even recorded:
# describing them would cost requests for files the MIME filter drops.
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff")
ACCEPTED_MIMES = frozenset({"image/jpeg", "image/png", "image/webp", "image/tiff"})
SERVED_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
LICENSE_FAMILIES = ("cc0", "pd", "cc-by", "cc-by-sa", "other")

EXTMETADATA_FIELDS = (
    "License",
    "LicenseShortName",
    "LicenseUrl",
    "UsageTerms",
    "AttributionRequired",
    "Copyrighted",
    "Restrictions",
    "Artist",
    "Credit",
    "ImageDescription",
    "ObjectName",
    "DateTimeOriginal",
    "GPSLatitude",
    "GPSLongitude",
    "Categories",
)
KEPT_INFO = (
    "timestamp",
    "user",
    "size",
    "width",
    "height",
    "mime",
    "sha1",
    "url",
    "descriptionurl",
    "thumburl",
    "thumbwidth",
    "thumbheight",
)

PROVENANCE_COLUMNS = (
    "file",
    "pageid",
    "title",
    "source_url",
    "image_url",
    "author",
    "credit",
    "license",
    "license_family",
    "license_url",
    "usage_terms",
    "attribution_required",
    "restrictions",
    "description",
    "object_name",
    "date_taken",
    "gps_lat",
    "gps_lon",
    "uploader",
    "uploaded_at",
    "original_width",
    "original_height",
    "original_sha1",
    "width",
    "height",
    "bytes",
    "sha256",
    "grayscale",
    "series",
    "root",
    "category",
    "depth",
    "categories",
    "downloaded_at",
    "decision",
    "reason",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    title   TEXT PRIMARY KEY,
    root    TEXT NOT NULL,
    depth   INTEGER NOT NULL,
    listed  INTEGER NOT NULL DEFAULT 0,
    files   INTEGER,
    subcats INTEGER
);
CREATE TABLE IF NOT EXISTS files (
    pageid        INTEGER PRIMARY KEY,
    title         TEXT NOT NULL,
    root          TEXT NOT NULL,
    category      TEXT NOT NULL,
    depth         INTEGER NOT NULL,
    discovered_at TEXT NOT NULL,
    described_at  TEXT,
    missing       INTEGER NOT NULL DEFAULT 0,
    info          TEXT,
    decision      TEXT,
    reason        TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    invalid       INTEGER NOT NULL DEFAULT 0,
    path          TEXT,
    bytes         INTEGER,
    sha256        TEXT,
    width         INTEGER,
    height        INTEGER,
    grayscale     INTEGER,
    downloaded_at TEXT
);
CREATE INDEX IF NOT EXISTS files_by_described ON files (described_at);
CREATE INDEX IF NOT EXISTS files_by_decision ON files (decision);
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    phases        TEXT NOT NULL,
    user_agent    TEXT,
    config_sha256 TEXT NOT NULL,
    outcome       TEXT
);
"""


# --------------------------------------------------------------------------
# Configuration


@dataclass(frozen=True)
class Root:
    category: str
    depth: int
    limit: int | None


@dataclass(frozen=True)
class Filters:
    licenses: frozenset[str]
    min_short_side: int
    max_aspect: float
    max_per_series: int | None
    min_year: int | None = None


@dataclass(frozen=True)
class Config:
    roots: tuple[Root, ...]
    exclude: tuple[str, ...]
    filters: Filters
    sha256: str

    def root(self, category: str) -> Root | None:
        for root in self.roots:
            if root.category == category:
                return root
        return None

    def excluded_by(self, categories: Iterable[str]) -> str | None:
        """The first of `categories` an `exclude` pattern matches, if any."""
        for name in categories:
            folded = name.casefold()
            for pattern in self.exclude:
                if fnmatch.fnmatchcase(folded, pattern.casefold()):
                    return name
        return None


def normalize_category(name: str) -> str:
    """`Category:Aerial_photographs` -> `Aerial photographs`."""
    name = name.strip()
    if name.casefold().startswith("category:"):
        name = name.split(":", 1)[1]
    return " ".join(name.replace("_", " ").split())


def load_config(path: Path) -> Config:
    raw = path.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise SystemExit(f"{path}: {error}") from error

    unknown = set(data) - {"exclude", "filters", "root"}
    if unknown:
        raise SystemExit(f"{path}: unknown keys {sorted(unknown)}")

    roots: list[Root] = []
    for entry in data.get("root", []):
        extra = set(entry) - {"category", "depth", "limit"}
        if extra or "category" not in entry:
            raise SystemExit(f"{path}: [[root]] {entry} needs `category` only")
        limit = entry.get("limit")
        roots.append(
            Root(
                category=normalize_category(str(entry["category"])),
                depth=int(entry.get("depth", 0)),
                limit=None if limit is None else int(limit),
            )
        )
    if not roots:
        raise SystemExit(f"{path}: no [[root]]")
    names = [root.category for root in roots]
    if len(set(names)) != len(names):
        raise SystemExit(f"{path}: a root is listed twice")

    section = data.get("filters", {})
    extra = set(section) - {
        "licenses",
        "min_short_side",
        "max_aspect",
        "max_per_series",
        "min_year",
    }
    if extra:
        raise SystemExit(f"{path}: unknown [filters] keys {sorted(extra)}")
    licenses = frozenset(str(value) for value in section.get("licenses", []))
    if not licenses or not licenses <= set(LICENSE_FAMILIES):
        raise SystemExit(f"{path}: licenses must be some of {LICENSE_FAMILIES}")
    per_series = section.get("max_per_series")
    min_year = section.get("min_year")
    filters = Filters(
        licenses=licenses,
        min_short_side=int(section.get("min_short_side", 224)),
        max_aspect=float(section.get("max_aspect", 2.0)),
        max_per_series=None if per_series is None else int(per_series),
        min_year=None if min_year is None else int(min_year),
    )
    return Config(
        roots=tuple(roots),
        exclude=tuple(str(value) for value in data.get("exclude", [])),
        filters=filters,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


# --------------------------------------------------------------------------
# Small helpers


def log(message: str) -> None:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} {message}", flush=True)


def utc_now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def duration(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{seconds:02d}s"


def spread(pageid: int) -> int:
    """A fixed pseudo-random order over files, independent of discovery order.

    Limits and caps keep the first files in this order, so a root's limit
    samples all of its subcategories instead of filling up with the first
    ones listed, and every run makes the same choice.
    """
    return (pageid * 2654435761) % 2**32


class _TextExtractor(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div", "li", "td"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: str) -> str:
    """The text of an `extmetadata` value, which is often HTML."""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return " ".join("".join(parser.parts).split())


_DATE = re.compile(r"(\d{4})[-:](\d{2})[-:](\d{2})")


def first_date(value: str) -> str:
    match = _DATE.search(value)
    return "-".join(match.groups()) if match else ""


_YEAR = re.compile(r"(?<!\d)(1[89]\d\d|20\d\d)(?!\d)")


def capture_year(meta: Mapping[str, str]) -> int | None:
    """The year a photo was taken: from its capture date, else from its name.

    Old photographs often have no capture date and a year in the title
    ("Victoria Park Swimming Pool 1957"). A long number such as a Flickr id
    is not read as a year.
    """
    for field in ("DateTimeOriginal", "ObjectName"):
        match = _YEAR.search(html_to_text(meta.get(field, "")))
        if match:
            return int(match.group(1))
    return None


def split_categories(value: str) -> list[str]:
    return [normalize_category(name) for name in value.split("|") if name.strip()]


def license_family(meta: Mapping[str, str]) -> str:
    """One of LICENSE_FAMILIES, from the machine-readable `License` code.

    `License` is a code such as `cc-by-sa-4.0` or `cc-by-sa-3.0-de`;
    `LicenseShortName` (`CC BY-SA 4.0`) is the fallback when it is absent.
    """
    for value in (meta.get("License", ""), meta.get("LicenseShortName", "")):
        code = "-".join(value.casefold().split())
        if not code:
            continue
        if code.startswith(("cc0", "cc-zero")):
            return "cc0"
        if code.startswith(("pd", "public-domain")):
            return "pd"
        if code.startswith(("cc-by-nc", "cc-by-nd")):
            return "other"
        if code.startswith("cc-by-sa"):
            return "cc-by-sa"
        if code.startswith("cc-by"):
            return "cc-by"
        return "other"
    return "other"


def series_key(info: Mapping[str, Any]) -> str:
    """Author and capture day: one drone session, as near as metadata tells."""
    meta: Mapping[str, str] = info.get("extmetadata", {})
    who = html_to_text(meta.get("Artist", "")) or str(info.get("user", "")) or "?"
    day = first_date(meta.get("DateTimeOriginal", ""))
    day = day or first_date(str(info.get("timestamp", ""))) or "?"
    return f"{who.casefold()} | {day}"


def is_grayscale(image: Image.Image) -> bool:
    if image.mode in {"1", "L", "LA", "I", "I;16", "F"}:
        return True
    red, green, blue = image.convert("RGB").split()
    drift = (
        ImageStat.Stat(ImageChops.difference(red, green)).mean[0]
        + ImageStat.Stat(ImageChops.difference(green, blue)).mean[0]
    )
    # JPEG noise leaves a gray photograph a little off-gray, never by this much.
    return drift < 2.0


# --------------------------------------------------------------------------
# HTTP


class ResponseTooLargeError(Exception):
    pass


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


def _lower(headers: Any) -> dict[str, str]:
    return {str(name).lower(): str(value) for name, value in headers.items()}


def http_get(url: str, user_agent: str, *, max_bytes: int, gzip_ok: bool) -> Response:
    """One GET. HTTP errors come back as a status; network errors raise."""
    headers = {"User-Agent": user_agent}
    if gzip_ok:
        headers["Accept-Encoding"] = "gzip"
    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=60)
    except urllib.error.HTTPError as error:
        try:
            return Response(error.code, _lower(error.headers), error.read(65536))
        finally:
            error.close()
    with response:
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ResponseTooLargeError(f"more than {max_bytes} bytes")
        found = _lower(response.headers)
        if found.get("content-encoding") == "gzip":
            body = gzip.decompress(body)
        return Response(int(response.status), found, body)


def retry_after(headers: Mapping[str, str], default: float) -> float:
    value = headers.get("retry-after")
    if not value:
        return default
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
            seconds = (when - datetime.datetime.now(datetime.UTC)).total_seconds()
        except (TypeError, ValueError):
            seconds = default
    return min(max(seconds, 1.0), 600.0)


NETWORK_ERRORS = (OSError, http.client.HTTPException)


class ApiError(Exception):
    pass


class ApiClient:
    """Serial requests to the Commons Action API, paced by the robot policy."""

    def __init__(self, user_agent: str) -> None:
        self.user_agent = user_agent
        self.requests = 0
        self._ready_at = 0.0

    def get(self, params: Mapping[str, str]) -> dict[str, Any]:
        query = {**params, "format": "json", "formatversion": "2", "maxlag": "5"}
        url = f"{API_URL}?{urllib.parse.urlencode(query)}"
        backoff = 5.0
        for _ in range(10):
            self._wait()
            started = time.monotonic()
            try:
                response = http_get(
                    url, self.user_agent, max_bytes=MAX_API_BYTES, gzip_ok=True
                )
            except (*NETWORK_ERRORS, ResponseTooLargeError) as error:
                self._done(started)
                log(f"api: {error!r}; again in {backoff:.0f} s")
                self._rest(backoff)
                backoff = min(backoff * 2, 300.0)
                continue
            self._done(started)
            self.requests += 1
            took = time.monotonic() - started
            if took > 10:
                log(f"api: slow answer, {took:.0f} s")
            if response.status == 429 or response.status >= 500:
                pause = retry_after(response.headers, backoff)
                log(f"api: HTTP {response.status}; again in {pause:.0f} s")
                self._rest(pause)
                backoff = min(backoff * 2, 300.0)
                continue
            if response.status != 200:
                raise ApiError(f"HTTP {response.status}: {response.body[:300]!r}")
            data = json.loads(response.body)
            if not isinstance(data, dict):
                raise ApiError(f"unexpected answer: {response.body[:300]!r}")
            problem = data.get("error")
            if isinstance(problem, dict):
                if problem.get("code") == "maxlag":
                    pause = retry_after(response.headers, 5.0)
                    log(f"api: database lag; again in {pause:.0f} s")
                    self._rest(pause)
                    continue
                raise ApiError(f"{problem.get('code')}: {problem.get('info')}")
            return data
        raise ApiError(f"gave up after 10 attempts: {url}")

    def get_all(self, params: Mapping[str, str]) -> Iterator[dict[str, Any]]:
        """Every page of a query, following `continue`."""
        cont: dict[str, str] = {}
        while True:
            data = self.get({**params, **cont})
            yield data
            following = data.get("continue")
            if not isinstance(following, dict):
                return
            cont = {str(key): str(value) for key, value in following.items()}

    def _wait(self) -> None:
        pause = self._ready_at - time.monotonic()
        if pause > 0:
            time.sleep(pause)

    def _done(self, started: float) -> None:
        # One request a second; five seconds of rest after a slow one.
        now = time.monotonic()
        rest = 5.0 if now - started > 1.0 else 0.0
        self._ready_at = max(started + 1.0, now + rest)

    def _rest(self, seconds: float) -> None:
        self._ready_at = max(self._ready_at, time.monotonic() + seconds)


class MediaThrottle:
    """Shared by the download workers: one pause for both, and a bandwidth cap."""

    def __init__(self, max_mbps: float) -> None:
        self._lock = threading.Lock()
        self._paused_until = 0.0
        self._window: deque[tuple[float, int]] = deque()
        self._cap = max_mbps * 1_000_000 / 8 * 10  # bytes per 10 s
        self._stopped = threading.Event()

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    def stop(self) -> None:
        self._stopped.set()

    def wait_turn(self) -> None:
        while not self.stopped:
            with self._lock:
                now = time.monotonic()
                while self._window and self._window[0][0] < now - 10:
                    self._window.popleft()
                pause = self._paused_until - now
                if sum(size for _, size in self._window) >= self._cap:
                    pause = max(pause, self._window[0][0] + 10 - now)
            if pause <= 0:
                return
            time.sleep(min(pause, 1.0))

    def count(self, size: int) -> None:
        with self._lock:
            self._window.append((time.monotonic(), size))

    def pause(self, seconds: float) -> None:
        with self._lock:
            self._paused_until = max(self._paused_until, time.monotonic() + seconds)


# --------------------------------------------------------------------------
# Phase 1: discover


def check_roots(api: ApiClient, config: Config) -> None:
    names = [root.category for root in config.roots]
    empty: list[str] = []
    for index in range(0, len(names), 50):
        titles = "|".join(f"Category:{name}" for name in names[index : index + 50])
        data = api.get({"action": "query", "prop": "categoryinfo", "titles": titles})
        for page in data.get("query", {}).get("pages", []):
            if not (page.get("categoryinfo") or {}).get("size"):
                empty.append(normalize_category(str(page.get("title", "?"))))
    if empty:
        raise SystemExit(
            "these roots do not exist on Commons, or are empty: " + "; ".join(empty)
        )


def list_category(
    db: sqlite3.Connection, api: ApiClient, title: str, root: str, depth: int
) -> tuple[int, int]:
    files = subcats = 0
    now = utc_now()
    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": f"Category:{title}",
        "cmtype": "subcat|file",
        "cmprop": "ids|title|type",
        "cmlimit": "500",
    }
    for data in api.get_all(params):
        for member in data.get("query", {}).get("categorymembers", []):
            kind = member.get("type")
            name = str(member.get("title", ""))
            if kind == "subcat":
                subcats += 1
                db.execute(
                    "INSERT OR IGNORE INTO categories (title, root, depth)"
                    " VALUES (?, ?, ?)",
                    (normalize_category(name), root, depth + 1),
                )
            elif kind == "file" and name.casefold().endswith(IMAGE_SUFFIXES):
                files += 1
                db.execute(
                    "INSERT OR IGNORE INTO files"
                    " (pageid, title, root, category, depth, discovered_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (int(member["pageid"]), name, root, title, depth, now),
                )
    db.execute(
        "UPDATE categories SET listed = 1, files = ?, subcats = ? WHERE title = ?",
        (files, subcats, title),
    )
    db.commit()
    return files, subcats


def discover(db: sqlite3.Connection, api: ApiClient, config: Config) -> None:
    check_roots(api, config)
    for root in config.roots:
        db.execute(
            "INSERT OR IGNORE INTO categories (title, root, depth) VALUES (?, ?, 0)",
            (root.category, root.category),
        )
    db.commit()

    started = time.monotonic()
    listed = 0
    while True:
        # Breadth first, one depth at a time, so an interrupted walk resumes
        # from the categories still unlisted.
        frontier: list[tuple[str, str, int]] = []
        for title, root_name, depth in db.execute(
            "SELECT title, root, depth FROM categories WHERE listed = 0"
            " ORDER BY depth, root, title"
        ):
            owner = config.root(root_name)
            if owner is None or depth > owner.depth:
                continue
            if depth > 0 and config.excluded_by([title]):
                continue
            frontier.append((title, root_name, depth))
        if not frontier:
            break
        for title, root_name, depth in frontier:
            files, subcats = list_category(db, api, title, root_name, depth)
            listed += 1
            log(
                f"discover: [{root_name} | depth {depth}] {title}:"
                f" {files} images, {subcats} subcategories"
            )

    known = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    elapsed = duration(time.monotonic() - started)
    log(
        f"discover: {listed} categories listed in {elapsed}"
        f" ({api.requests} API requests so far); {known} image files known"
    )


# --------------------------------------------------------------------------
# Phase 2: describe


def compact_info(entry: Mapping[str, Any]) -> dict[str, Any]:
    info: dict[str, Any] = {key: entry[key] for key in KEPT_INFO if key in entry}
    info["extmetadata"] = {
        str(name): str(field.get("value", ""))
        for name, field in (entry.get("extmetadata") or {}).items()
        if isinstance(field, dict)
    }
    return info


def fetch_infos(api: ApiClient, pageids: Sequence[int]) -> dict[int, dict[str, Any]]:
    params = {
        "action": "query",
        "pageids": "|".join(str(pageid) for pageid in pageids),
        "prop": "imageinfo",
        "iiprop": "timestamp|user|size|mime|sha1|url|extmetadata",
        "iiurlwidth": str(THUMB_WIDTH),
        "iiextmetadatafilter": "|".join(EXTMETADATA_FIELDS),
        "iiextmetadatalanguage": "en",
    }
    infos: dict[int, dict[str, Any]] = {}
    for data in api.get_all(params):
        for page in data.get("query", {}).get("pages", []):
            entries = page.get("imageinfo")
            if entries and "pageid" in page:
                infos[int(page["pageid"])] = compact_info(entries[0])
    return infos


def describe(db: sqlite3.Connection, api: ApiClient) -> None:
    # In `spread` order, not by pageid (which is upload order): the files
    # described so far are always a random sample of all of them, so
    # `--phase status` means something before describe ends, and stopping
    # early leaves a smaller set rather than a biased one.
    pending = sorted(
        (
            int(row[0])
            for row in db.execute("SELECT pageid FROM files WHERE described_at IS NULL")
        ),
        key=spread,
    )
    log(f"describe: {len(pending)} files without metadata")
    started = time.monotonic()
    for index in range(0, len(pending), DESCRIBE_BATCH):
        batch = pending[index : index + DESCRIBE_BATCH]
        infos = fetch_infos(api, batch)
        now = utc_now()
        for pageid in batch:
            info = infos.get(pageid)
            if info is None:
                # Deleted since discovery, or a page with no file behind it.
                db.execute(
                    "UPDATE files SET described_at = ?, missing = 1 WHERE pageid = ?",
                    (now, pageid),
                )
            else:
                db.execute(
                    "UPDATE files SET described_at = ?, missing = 0, info = ?"
                    " WHERE pageid = ?",
                    (now, json.dumps(info, ensure_ascii=False), pageid),
                )
        db.commit()
        done = index + len(batch)
        if (index // DESCRIBE_BATCH) % 20 == 19 or done == len(pending):
            elapsed = time.monotonic() - started
            remaining = elapsed / done * (len(pending) - done)
            log(
                f"describe: {done}/{len(pending)} in {duration(elapsed)},"
                f" about {duration(remaining)} to go"
            )


# --------------------------------------------------------------------------
# Filters


def static_reason(info: Mapping[str, Any], config: Config) -> str | None:
    """Why a file fails the filters that look at it alone, if it does."""
    meta: Mapping[str, str] = info.get("extmetadata", {})
    mime = str(info.get("mime", ""))
    if mime not in ACCEPTED_MIMES:
        return f"mime: {mime or 'unknown'}"
    excluded = config.excluded_by(split_categories(meta.get("Categories", "")))
    if excluded:
        return f"excluded category: {excluded}"
    family = license_family(meta)
    if family not in config.filters.licenses:
        return f"license: {family} ({meta.get('LicenseShortName') or '?'})"
    restrictions = meta.get("Restrictions", "").strip()
    if restrictions:
        return f"restrictions: {restrictions}"
    if config.filters.min_year is not None:
        year = capture_year(meta)
        if year is not None and year < config.filters.min_year:
            return f"too old: {year}"
    width = int(info.get("thumbwidth") or 0)
    height = int(info.get("thumbheight") or 0)
    if not info.get("thumburl") or not width or not height:
        return "thumbnail: none"
    aspect = max(width, height) / min(width, height)
    if aspect > config.filters.max_aspect:
        return f"aspect: {aspect:.2f}"
    if min(width, height) < config.filters.min_short_side:
        return f"too small: {width}x{height}"
    return None


def evaluate(db: sqlite3.Connection, config: Config) -> Counter[str]:
    """Decide every described file again; count the decisions by kind.

    Downloaded files go first, so they keep their places under the duplicate
    check, the series cap and the root limits; the rest follow in `spread`
    order.
    """
    rows = db.execute(
        "SELECT pageid, root, info, downloaded_at, invalid, attempts, last_error,"
        " grayscale FROM files WHERE info IS NOT NULL AND missing = 0"
    ).fetchall()
    rows.sort(key=lambda row: (row[3] is None, spread(row[0])))

    seen: dict[str, int] = {}
    series: Counter[str] = Counter()
    per_root: Counter[str] = Counter()
    tally: Counter[str] = Counter()
    updates: list[tuple[str, str | None, int]] = []
    for row in rows:
        pageid, root_name, raw, downloaded_at, invalid, attempts, last_error = row[:7]
        info = json.loads(raw)
        reason = static_reason(info, config)
        if reason is None and row[7]:
            # Only known once downloaded: the metadata does not say it.
            reason = "grayscale"
        if reason is None and invalid:
            reason = f"bad download: {last_error}"
        if reason is None and downloaded_at is None and attempts >= MAX_ATTEMPTS:
            reason = f"download failed: {last_error}"
        if reason is None:
            sha1 = str(info.get("sha1") or "")
            if sha1 in seen:
                reason = f"duplicate: of {seen[sha1]}"
            elif sha1:
                seen[sha1] = pageid
        if reason is None and config.filters.max_per_series is not None:
            key = series_key(info)
            if series[key] >= config.filters.max_per_series:
                reason = f"series cap: {key}"
            else:
                series[key] += 1
        if reason is None:
            root = config.root(root_name)
            if root is None:
                reason = f"root not configured: {root_name}"
            elif root.limit is not None and per_root[root_name] >= root.limit:
                reason = f"root limit: {root.limit}"
            else:
                per_root[root_name] += 1
        updates.append(("accepted" if reason is None else "rejected", reason, pageid))
        tally["accepted" if reason is None else reason.split(":", 1)[0]] += 1
    db.executemany(
        "UPDATE files SET decision = ?, reason = ? WHERE pageid = ?", updates
    )
    db.commit()
    return tally


# --------------------------------------------------------------------------
# Phase 3: download


@dataclass(frozen=True)
class Fetched:
    path: str | None = None
    size: int = 0
    sha256: str = ""
    width: int = 0
    height: int = 0
    grayscale: bool = False
    error: str | None = None
    permanent: bool = False
    fatal: bool = False


class DownloadsStoppedError(Exception):
    pass


def keep_thumbnail(
    response: Response, width: int, height: int, out_dir: Path, pageid: int
) -> Fetched:
    content_type = response.headers.get("content-type", "")
    content_type = content_type.split(";", 1)[0].strip().lower()
    extension = SERVED_EXTENSIONS.get(content_type)
    if extension is None:
        return Fetched(error=f"served as {content_type or 'nothing'}", permanent=True)
    try:
        with Image.open(io.BytesIO(response.body)) as image:
            image.load()
            found = image.size
            gray = is_grayscale(image)
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        return Fetched(error=f"not a readable image: {error}", permanent=True)
    if abs(found[0] - width) > 1 or abs(found[1] - height) > 1:
        return Fetched(
            error=f"{found[0]}x{found[1]}, the API said {width}x{height}",
            permanent=True,
        )
    relative = Path("images") / f"{pageid % 100:02d}" / f"{pageid}{extension}"
    target = out_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    partial.write_bytes(response.body)
    os.replace(partial, target)
    return Fetched(
        path=relative.as_posix(),
        size=len(response.body),
        sha256=hashlib.sha256(response.body).hexdigest(),
        width=found[0],
        height=found[1],
        grayscale=gray,
    )


def fetch_thumbnail(
    url: str,
    width: int,
    height: int,
    out_dir: Path,
    pageid: int,
    user_agent: str,
    throttle: MediaThrottle,
) -> Fetched:
    backoff = 5.0
    error = "not attempted"
    for _ in range(4):
        throttle.wait_turn()
        if throttle.stopped:
            return Fetched(error="stopped")
        try:
            response = http_get(
                url, user_agent, max_bytes=MAX_THUMB_BYTES, gzip_ok=False
            )
        except ResponseTooLargeError:
            return Fetched(error="larger than 10 MB", permanent=True)
        except NETWORK_ERRORS as caught:
            error = f"network: {caught!r}"
            throttle.pause(backoff)
            backoff = min(backoff * 2, 300.0)
            continue
        throttle.count(len(response.body))
        if response.status == 200:
            return keep_thumbnail(response, width, height, out_dir, pageid)
        error = f"HTTP {response.status}"
        if response.status in {404, 410}:
            return Fetched(error=error, permanent=True)
        if response.status == 403:
            return Fetched(error=f"{error}: refused; check --contact", fatal=True)
        if response.status == 429 or response.status >= 500:
            throttle.pause(retry_after(response.headers, backoff))
            backoff = min(backoff * 2, 300.0)
            continue
        return Fetched(error=error, permanent=True)
    return Fetched(error=error)


def record(db: sqlite3.Connection, pageid: int, result: Fetched) -> None:
    if result.error is None:
        db.execute(
            "UPDATE files SET path = ?, bytes = ?, sha256 = ?, width = ?, height = ?,"
            " grayscale = ?, downloaded_at = ?, attempts = attempts + 1,"
            " last_error = NULL WHERE pageid = ?",
            (
                result.path,
                result.size,
                result.sha256,
                result.width,
                result.height,
                int(result.grayscale),
                utc_now(),
                pageid,
            ),
        )
    else:
        db.execute(
            "UPDATE files SET attempts = attempts + 1, last_error = ?, invalid = ?"
            " WHERE pageid = ?",
            (result.error, int(result.permanent), pageid),
        )
    db.commit()


def download(
    db: sqlite3.Connection,
    out_dir: Path,
    config: Config,
    user_agent: str,
    limit: int | None,
    max_mbps: float,
) -> None:
    log("download: filters -> " + summarize(evaluate(db, config)))
    queue = [
        (int(pageid), json.loads(raw))
        for pageid, raw in db.execute(
            "SELECT pageid, info FROM files"
            " WHERE decision = 'accepted' AND downloaded_at IS NULL"
        )
    ]
    queue.sort(key=lambda item: spread(item[0]))
    if limit is not None:
        queue = queue[:limit]
    log(f"download: {len(queue)} thumbnails to fetch in this run")
    # Left by a run that was killed while writing; their files were not kept.
    for stray in (out_dir / "images").glob("*/*.part"):
        stray.unlink()

    throttle = MediaThrottle(max_mbps)
    pending = iter(queue)
    running: dict[Future[Fetched], int] = {}
    fetched = failed = consecutive = 0
    total_bytes = 0
    started = last_report = time.monotonic()

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:

        def submit_next() -> None:
            item = next(pending, None)
            if item is None:
                return
            pageid, info = item
            future = pool.submit(
                fetch_thumbnail,
                str(info["thumburl"]),
                int(info["thumbwidth"]),
                int(info["thumbheight"]),
                out_dir,
                pageid,
                user_agent,
                throttle,
            )
            running[future] = pageid

        try:
            for _ in range(DOWNLOAD_WORKERS * 2):
                submit_next()
            while running:
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    pageid = running.pop(future)
                    result = future.result()
                    record(db, pageid, result)
                    if result.error is None:
                        fetched += 1
                        total_bytes += result.size
                        consecutive = 0
                    else:
                        failed += 1
                        consecutive += 1
                        log(f"download: {pageid}: {result.error}")
                        if result.fatal:
                            raise DownloadsStoppedError(result.error)
                        if consecutive >= MAX_CONSECUTIVE_FAILURES:
                            raise DownloadsStoppedError(
                                f"{consecutive} failures in a row, the last:"
                                f" {result.error}"
                            )
                    submit_next()
                now = time.monotonic()
                if now - last_report >= 60 or not running:
                    last_report = now
                    elapsed = now - started
                    finished = fetched + failed
                    rate = finished / elapsed if elapsed else 0.0
                    remaining = (len(queue) - finished) / rate if rate else 0.0
                    log(
                        f"download: {finished}/{len(queue)} ({failed} failed),"
                        f" {total_bytes / 1e6:.1f} MB in {duration(elapsed)},"
                        f" {rate:.2f} images/s, about {duration(remaining)} to go"
                    )
        except BaseException:
            # Let the workers return at once instead of finishing their pauses.
            throttle.stop()
            raise

    elapsed = time.monotonic() - started
    if fetched:
        log(
            f"download: {fetched} thumbnails, {total_bytes / 1e6:.1f} MB"
            f" ({total_bytes / fetched / 1e3:.0f} KB each) in {duration(elapsed)}"
        )


# --------------------------------------------------------------------------
# Phase 4: export, and status


def provenance_row(row: sqlite3.Row) -> list[object]:
    info = json.loads(row["info"])
    meta: Mapping[str, str] = info.get("extmetadata", {})
    return [
        row["path"],
        row["pageid"],
        row["title"],
        info.get("descriptionurl", ""),
        info.get("thumburl", ""),
        html_to_text(meta.get("Artist", "")),
        html_to_text(meta.get("Credit", "")),
        meta.get("LicenseShortName", ""),
        license_family(meta),
        meta.get("LicenseUrl", ""),
        meta.get("UsageTerms", ""),
        meta.get("AttributionRequired", ""),
        meta.get("Restrictions", ""),
        html_to_text(meta.get("ImageDescription", "")),
        html_to_text(meta.get("ObjectName", "")),
        html_to_text(meta.get("DateTimeOriginal", "")),
        meta.get("GPSLatitude", ""),
        meta.get("GPSLongitude", ""),
        info.get("user", ""),
        info.get("timestamp", ""),
        info.get("width", ""),
        info.get("height", ""),
        info.get("sha1", ""),
        row["width"],
        row["height"],
        row["bytes"],
        row["sha256"],
        row["grayscale"],
        series_key(info),
        row["root"],
        row["category"],
        row["depth"],
        meta.get("Categories", ""),
        row["downloaded_at"],
        row["decision"],
        row["reason"] or "",
    ]


def export(db: sqlite3.Connection, out_dir: Path, config: Config) -> None:
    evaluate(db, config)
    target = out_dir / PROVENANCE_NAME
    partial = target.with_name(target.name + ".part")
    count = 0
    with partial.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(PROVENANCE_COLUMNS)
        for row in db.execute(
            "SELECT * FROM files WHERE downloaded_at IS NOT NULL ORDER BY pageid"
        ):
            writer.writerow(provenance_row(row))
            count += 1
    os.replace(partial, target)
    log(f"export: {count} rows in {target}")


def summarize(tally: Counter[str]) -> str:
    accepted = tally.get("accepted", 0)
    rejected = ", ".join(
        f"{kind} {count}" for kind, count in tally.most_common() if kind != "accepted"
    )
    return f"{accepted} accepted; rejected: {rejected or 'none'}"


def status(db: sqlite3.Connection, config: Config) -> None:
    tally = evaluate(db, config)

    def one(sql: str) -> Any:
        return db.execute(sql).fetchone()[0]

    print(
        f"categories: {one('SELECT COUNT(*) FROM categories WHERE listed = 1')}"
        f" listed, {one('SELECT COUNT(*) FROM categories')} known"
    )
    print(
        f"image files: {one('SELECT COUNT(*) FROM files')} known,"
        f" {one('SELECT COUNT(*) FROM files WHERE described_at IS NOT NULL')}"
        f" described, {one('SELECT COUNT(*) FROM files WHERE missing = 1')} gone"
    )
    print("decisions: " + summarize(tally))

    print("\nby root: known / accepted / downloaded (limit)")
    for root in config.roots:
        row = db.execute(
            "SELECT COUNT(*), SUM(decision = 'accepted'),"
            " SUM(decision = 'accepted' AND downloaded_at IS NOT NULL)"
            " FROM files WHERE root = ?",
            (root.category,),
        ).fetchone()
        limit = "" if root.limit is None else f" ({root.limit})"
        print(
            f"  {row[0]:>7} {row[1] or 0:>7} {row[2] or 0:>7}{limit}  {root.category}"
        )

    families: Counter[str] = Counter()
    for (raw,) in db.execute("SELECT info FROM files WHERE decision = 'accepted'"):
        families[license_family(json.loads(raw).get("extmetadata", {}))] += 1
    print(
        "\naccepted, by license: "
        + ", ".join(
            f"{family} {families[family]}"
            for family in LICENSE_FAMILIES
            if families[family]
        )
    )

    reasons = db.execute(
        "SELECT reason, COUNT(*) FROM files WHERE decision = 'rejected'"
        " AND reason NOT LIKE 'series cap:%' AND reason NOT LIKE 'duplicate:%'"
        " GROUP BY reason ORDER BY COUNT(*) DESC LIMIT 15"
    ).fetchall()
    if reasons:
        print("\nmost common rejections:")
        for reason, count in reasons:
            print(f"  {count:>7}  {reason}")

    row = db.execute(
        "SELECT COUNT(*), SUM(bytes), SUM(grayscale) FROM files"
        " WHERE downloaded_at IS NOT NULL"
    ).fetchone()
    if row[0]:
        print(
            f"\ndownloaded: {row[0]} thumbnails, {(row[1] or 0) / 1e6:.1f} MB,"
            f" {(row[1] or 0) / row[0] / 1e3:.0f} KB each, {row[2] or 0} grayscale"
        )
    pending = one(
        "SELECT COUNT(*) FROM files"
        " WHERE decision = 'accepted' AND downloaded_at IS NULL"
    )
    print(f"pending downloads: {pending}")


# --------------------------------------------------------------------------
# Entry point


def open_database(path: Path) -> sqlite3.Connection:
    # `--phase status` may run in a second terminal while another phase
    # writes; each waits for the other's transaction instead of failing.
    db = sqlite3.connect(path, timeout=60)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
    db.executescript(SCHEMA)
    return db


def user_agent_for(contact: str | None) -> str:
    if not contact or not (
        "@" in contact or contact.startswith(("http://", "https://"))
    ):
        raise SystemExit(
            "--contact must be an email address or a URL: Wikimedia requires"
            " automated clients to say how their operator can be reached"
        )
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    return f"{CLIENT} ({contact}) Python-urllib/{python}"


def parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect aerial and drone photographs from Wikimedia Commons"
        " (RFC-042 section 4)."
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="output directory; created if absent"
    )
    parser.add_argument(
        "--contact",
        help="an email address or URL for the User-Agent (required by Wikimedia"
        " for every phase that uses the network)",
    )
    parser.add_argument(
        "--categories",
        type=Path,
        default=DEFAULT_CATEGORIES,
        help=f"roots, exclusions and filters (default: {DEFAULT_CATEGORIES.name})",
    )
    parser.add_argument(
        "--phase",
        choices=("all", "discover", "describe", "download", "export", "status"),
        default="all",
    )
    parser.add_argument(
        "--limit", type=int, help="download at most this many thumbnails in this run"
    )
    parser.add_argument(
        "--max-mbps",
        type=float,
        default=DEFAULT_MAX_MBPS,
        help=f"download bandwidth cap (default {DEFAULT_MAX_MBPS:g};"
        " Wikimedia's is 25)",
    )
    args = parser.parse_args(argv)
    if not 0 < args.max_mbps <= 25:
        parser.error("--max-mbps must be above 0 and at most 25")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = parse_arguments(argv)
    config = load_config(args.categories)
    phases = (
        ["discover", "describe", "download", "export"]
        if args.phase == "all"
        else [args.phase]
    )
    needs_network = bool({"discover", "describe", "download"} & set(phases))
    user_agent = user_agent_for(args.contact) if needs_network else None

    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    db = open_database(out_dir / DATABASE_NAME)
    run = db.execute(
        "INSERT INTO runs (started_at, phases, user_agent, config_sha256)"
        " VALUES (?, ?, ?, ?)",
        (utc_now(), ",".join(phases), user_agent, config.sha256),
    ).lastrowid
    db.commit()

    api = ApiClient(user_agent) if user_agent else None
    outcome = "done"
    code = 0
    try:
        for phase in phases:
            if phase == "discover" and api:
                discover(db, api, config)
            elif phase == "describe" and api:
                describe(db, api)
            elif phase == "download" and user_agent:
                download(db, out_dir, config, user_agent, args.limit, args.max_mbps)
            elif phase == "export":
                export(db, out_dir, config)
            elif phase == "status":
                status(db, config)
    except KeyboardInterrupt:
        outcome, code = "interrupted", 130
        log("interrupted; the same command continues where this run stopped")
    except DownloadsStoppedError as stop:
        outcome, code = f"stopped: {stop}", 1
        log(f"download: stopped. {stop}")
    except ApiError as error:
        outcome, code = f"api error: {error}", 1
        log(f"api: {error}")
    finally:
        if api:
            outcome += f" ({api.requests} API requests)"
        db.execute(
            "UPDATE runs SET finished_at = ?, outcome = ? WHERE id = ?",
            (utc_now(), outcome, run),
        )
        db.commit()
        db.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
