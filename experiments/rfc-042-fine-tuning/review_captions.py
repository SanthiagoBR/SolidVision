"""A local page to write and review captions (RFC-042 sections 4.3 and 5.1).

RFC-042 needs human text in three places, and this page is where it is
written, one photo at a time, over the photos `collect_commons.py` downloaded
and accepted:

- **Escrever legendas** (`caption`): training captions written by hand, in
  English, for the cases the RFC-023 bake-off failed on: streets, urban
  scenes, negation (section 4.3, item 3). Each one is tagged with its focus,
  because the share of negation captions is a parameter of the experiment
  (section 4.5).
- **Revisar VLM** (`review`): a random sample of the captions a
  vision-language model wrote, approved, corrected or rejected (section 4.3,
  item 2). The share corrected or rejected is the error rate that section
  leaves to be measured; the page shows it as it goes. The captions come from
  `vlm_captions.jsonl` in the collection directory, one JSON object per line:
  `{"pageid": 123, "text": "...", "model": "..."}`. The step that writes that
  file does not exist yet.
- **Consultas de avaliação** (`query`): queries written by hand for the new
  oblique evaluation set (section 5.1.2), in Portuguese (which the app
  translates, as it does for a user) and optionally in English (section 5.2
  measures both). A model may draft them first, in `query_drafts.jsonl` in
  the collection directory (`{"pageid": 123, "pt": "...", "en": "...",
  "model": "..."}` per line): drafted photos come first, the draft fills the
  fields, and the saved query records the draft and whether it was approved
  as is or edited. Model-drafted queries can share wording with
  model-written captions, so the analysis keeps them apart from the ones
  written from scratch (`"source": "human"`). Queries saved before drafts
  existed carry no `source` and were all written from scratch. Some photos
  also get a second Portuguese query with southern Brazilian vocabulary
  (`pt_south`: sanga, potreiro, coxilha, açude...), searching for the same
  photo: the pair measures what the translator does to regional words,
  which no fine-tuning of CLIP can fix, so it is scored apart.

**Evaluation photos never train.** A photo with a query, and every photo of
its series (same author, same capture day: near-identical shots from one
drone session), is never offered for a training caption, and a series that
already has training captions is never offered for a query (RFC-042 section
4.6). Each record carries its series so the packaging step can hold the whole
series out of the training shards, VLM captions included.

**Where the work goes.** `captions.jsonl`, next to this script by default,
is meant to be committed: hours of human work in a few megabytes, while the
images and the database can be rebuilt by the collector. It is append-only,
one JSON object per line, and for each photo and kind the last line wins, so
correcting a caption appends instead of rewriting:

    {"at": "...", "kind": "caption", "pageid": 123, "title": "...",
     "series": "...", "text": "...", "lang": "en", "source": "human",
     "focus": "negation"}
    {"at": "...", "kind": "review", ..., "vlm": "...", "model": "...",
     "verdict": "approved" | "edited" | "rejected", "text": "..."}
    {"at": "...", "kind": "query", ..., "pt": "...", "en": "...", "pt_south": "...",
     "source": "human" | "model draft", "draft_pt": "...", "verdict": ...}
    {"at": "...", "kind": "caption", "pageid": 123, ..., "deleted": true}

Skipped photos are not work and do not go there: they are kept in the
collection's database, per mode and filter.

The page binds to 127.0.0.1 only, answers only requests addressed to that
host, and accepts writes only as JSON from its own origin, so another site
open in the browser cannot write captions through it.

    python experiments/rfc-042-fine-tuning/review_captions.py --out DIR

`DIR` is the collector's output directory. The page follows it while open:
new downloads, re-applied filters and new VLM or draft files show up on the
next photo, without a restart.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import threading
import webbrowser
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect_commons as commons  # noqa: E402

HERE = Path(__file__).resolve().parent
DEFAULT_CAPTIONS = HERE / "captions.jsonl"
PAGE = HERE / "review_captions.html"
VLM_NAME = "vlm_captions.jsonl"
DRAFTS_NAME = "query_drafts.jsonl"
DEFAULT_PORT = 8742

# Page mode -> the `kind` of the records it writes.
MODES = {"write": "caption", "review": "review", "queries": "query"}
FOCUS = ("street", "urban", "negation", "rural", "other")
MAX_TEXT = 1000
REVIEW_SHARE = 0.10  # RFC-042 section 4.3: review 5 to 10% of the VLM's captions
IMAGE_TYPES = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

# Commons housekeeping and camera categories, hidden from the page (they stay
# searchable): they say nothing about what the photo shows.
HOUSEKEEPING = (
    "taken with",
    "images with",
    "photographs with aspect",
    "iso speed",
    "f-number",
    "exposure time",
    "lens focal length",
    "files with",
    "files by",
    "files from",
    "self-published",
    "license migration",
    "flickr",
    "uploaded",
    "pages with",
    "cc-by",
    "cc-zero",
    "gfdl",
    "pd-",
    "media needing",
    "information field",
)

# Changes when a download lands or the collector re-applies its filters.
PHOTOS_SIGNATURE = (
    "SELECT COUNT(*), MAX(downloaded_at), TOTAL(pageid) FROM files"
    " WHERE downloaded_at IS NOT NULL AND decision = 'accepted'"
)

SKIPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_skips (
    pageid INTEGER NOT NULL,
    mode   TEXT NOT NULL,
    filter TEXT NOT NULL,
    at     TEXT NOT NULL,
    PRIMARY KEY (pageid, mode, filter)
);
"""


class BadRequestError(Exception):
    """A request the page can explain to the person using it."""


@dataclass(frozen=True)
class Photo:
    pageid: int
    path: str
    title: str
    description: str
    categories: tuple[str, ...]
    license: str
    author: str
    source_url: str
    series: str
    haystack: str


def load_photos(db: sqlite3.Connection) -> list[Photo]:
    """The downloaded photos the current filters accept, in `spread` order."""
    photos: list[Photo] = []
    for pageid, path, title, raw in db.execute(
        "SELECT pageid, path, title, info FROM files"
        " WHERE downloaded_at IS NOT NULL AND decision = 'accepted'"
    ):
        info = json.loads(raw)
        meta: Mapping[str, str] = info.get("extmetadata", {})
        name = str(title).removeprefix("File:")
        description = commons.html_to_text(meta.get("ImageDescription", ""))
        categories = tuple(commons.split_categories(meta.get("Categories", "")))
        photos.append(
            Photo(
                pageid=int(pageid),
                path=str(path),
                title=name,
                description=description,
                categories=categories,
                license=meta.get("LicenseShortName", ""),
                author=commons.html_to_text(meta.get("Artist", "")),
                source_url=str(info.get("descriptionurl", "")),
                series=commons.series_key(info),
                haystack=" ".join((name, description, *categories)).casefold(),
            )
        )
    photos.sort(key=lambda photo: commons.spread(photo.pageid))
    return photos


def load_vlm(path: Path) -> dict[int, dict[str, str]]:
    captions: dict[int, dict[str, str]] = {}
    if not path.exists():
        return captions
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            captions[int(record["pageid"])] = {
                "text": str(record["text"]),
                "model": str(record.get("model", "?")),
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            print(f"{path}:{number}: not a VLM caption, ignored", file=sys.stderr)
    return captions


def load_drafts(path: Path) -> dict[int, dict[str, str]]:
    """Query drafts a model wrote, for the person to correct: pt, en, model."""
    drafts: dict[int, dict[str, str]] = {}
    if not path.exists():
        return drafts
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            drafts[int(record["pageid"])] = {
                "pt": str(record.get("pt", "")),
                "en": str(record.get("en", "")),
                "pt_south": str(record.get("pt_south", "")),
                "model": str(record.get("model", "?")),
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            print(f"{path}:{number}: not a query draft, ignored", file=sys.stderr)
    return drafts


def _mtime(path: Path | None) -> int | None:
    if path is None:
        return None
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


class Ledger:
    """`captions.jsonl`: append-only; per photo and kind, the last line counts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._latest: dict[tuple[int, str], dict[str, Any]] = {}
        self._needs_newline = False
        if not path.exists():
            return
        text = path.read_text(encoding="utf-8")
        self._needs_newline = bool(text) and not text.endswith("\n")
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                self._apply(json.loads(line))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                print(f"{path}:{number}: unreadable line, ignored", file=sys.stderr)

    def _apply(self, record: dict[str, Any]) -> None:
        self._latest[(int(record["pageid"]), str(record["kind"]))] = record

    def add(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            if self._needs_newline:
                handle.write("\n")
                self._needs_newline = False
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._apply(record)

    def get(self, pageid: int, kind: str) -> dict[str, Any] | None:
        record = self._latest.get((pageid, kind))
        return None if record is None or record.get("deleted") else record

    def live(self) -> Iterator[tuple[int, str, dict[str, Any]]]:
        for (pageid, kind), record in self._latest.items():
            if not record.get("deleted"):
                yield pageid, kind, record


def clean_text(payload: Mapping[str, Any], key: str) -> str:
    text = " ".join(str(payload.get(key) or "").split())
    if len(text) > MAX_TEXT:
        raise BadRequestError(f"Texto longo demais (máximo {MAX_TEXT} caracteres).")
    return text


class Workbench:
    """The page's state: photos, the ledger, the VLM captions and the skips."""

    def __init__(
        self,
        photos: list[Photo],
        ledger: Ledger,
        vlm: dict[int, dict[str, str]],
        vlm_path: Path,
        db: sqlite3.Connection,
        out_dir: Path,
        drafts_path: Path | None = None,
    ) -> None:
        self.photos = photos
        self.by_id = {photo.pageid: photo for photo in photos}
        self.ledger = ledger
        self.vlm = vlm
        self.vlm_path = vlm_path
        self.db = db
        self.out_dir = out_dir.resolve()
        self.lock = threading.Lock()
        self.skips: set[tuple[int, str, str]] = {
            (int(pageid), str(mode), str(key))
            for pageid, mode, key in db.execute(
                "SELECT pageid, mode, filter FROM review_skips"
            )
        }
        self.drafts_path = drafts_path
        self.drafts = load_drafts(drafts_path) if drafts_path else {}
        self._photos_signature = self._signature()
        self._vlm_stamp = _mtime(vlm_path)
        self._drafts_stamp = _mtime(drafts_path)

    # -- keeping up with the collection while the page is open

    def _signature(self) -> tuple[Any, ...]:
        return tuple(self.db.execute(PHOTOS_SIGNATURE).fetchone())

    def _refresh(self) -> None:
        """Pick up new downloads, changed filters, and new VLM or draft files.

        Without this, a page started before a download or before the VLM
        captions were written showed the old state until it was restarted.
        """
        signature = self._signature()
        if signature != self._photos_signature:
            self.photos = load_photos(self.db)
            self.by_id = {photo.pageid: photo for photo in self.photos}
            self._photos_signature = signature
        stamp = _mtime(self.vlm_path)
        if stamp != self._vlm_stamp:
            self.vlm = load_vlm(self.vlm_path)
            self._vlm_stamp = stamp
        stamp = _mtime(self.drafts_path)
        if self.drafts_path is not None and stamp != self._drafts_stamp:
            self.drafts = load_drafts(self.drafts_path)
            self._drafts_stamp = stamp

    # -- reads

    def next(self, mode: str, query: str) -> dict[str, Any]:
        with self.lock:
            self._refresh()
            words = query.casefold().split()
            candidates = self._eligible(mode, words)
            if not candidates:
                return {
                    "item": None,
                    "remaining": 0,
                    "message": self._empty(mode, words),
                    "stats": self._stats(),
                }
            return {
                **self._item(mode, candidates[0], len(candidates)),
                "stats": self._stats(),
            }

    def item(self, mode: str, pageid: int) -> dict[str, Any]:
        with self.lock:
            self._refresh()
            return self._item(mode, self.photo(pageid), len(self._eligible(mode, [])))

    def photo(self, pageid: int) -> Photo:
        photo = self.by_id.get(pageid)
        if photo is None:
            raise BadRequestError("Esta foto não está entre as baixadas e aceitas.")
        return photo

    def image_path(self, pageid: int) -> Path:
        path = (self.out_dir / self.photo(pageid).path).resolve()
        if not path.is_relative_to(self.out_dir) or not path.is_file():
            raise BadRequestError("O arquivo desta foto não está na pasta da coleta.")
        return path

    def stats(self) -> dict[str, Any]:
        with self.lock:
            self._refresh()
            return self._stats()

    # -- writes

    def save(self, mode: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self.lock:
            photo = self.photo(_pageid(payload.get("pageid")))
            conflict = self._conflict(mode, photo)
            if conflict:
                raise BadRequestError(conflict)
            kind = MODES[mode]
            base: dict[str, Any] = {
                "at": commons.utc_now(),
                "kind": kind,
                "pageid": photo.pageid,
                "title": photo.title,
                "series": photo.series,
            }
            existing = self.ledger.get(photo.pageid, kind)
            record: dict[str, Any]
            if mode == "write":
                text = clean_text(payload, "text")
                if not text:
                    return self._delete(base, existing, "Escreva a legenda ou pule.")
                focus = payload.get("focus")
                if focus not in FOCUS:
                    raise BadRequestError("Escolha o foco da legenda.")
                record = {
                    **base,
                    "text": text,
                    "lang": "en",
                    "source": "human",
                    "focus": focus,
                }
            elif mode == "review":
                vlm = self.vlm.get(photo.pageid)
                if vlm is None:
                    raise BadRequestError("Esta foto não tem legenda do VLM.")
                if payload.get("reject"):
                    verdict, text = "rejected", ""
                else:
                    text = clean_text(payload, "text")
                    if not text:
                        raise BadRequestError(
                            "A legenda ficou vazia. Para descartá-la, use Rejeitar."
                        )
                    same = text == " ".join(vlm["text"].split())
                    verdict = "approved" if same else "edited"
                record = {
                    **base,
                    "vlm": vlm["text"],
                    "model": vlm["model"],
                    "verdict": verdict,
                    "text": text,
                }
            else:
                portuguese = clean_text(payload, "pt")
                english = clean_text(payload, "en")
                if not portuguese and not english:
                    return self._delete(
                        base, existing, "Escreva a consulta em português."
                    )
                record = {**base, "pt": portuguese, "en": english}
                south = clean_text(payload, "pt_south")
                if south:
                    record["pt_south"] = south
                draft = self.drafts.get(photo.pageid)
                if draft is None:
                    record["source"] = "human"
                else:
                    # Kept apart in the analysis: a model drafted these, so
                    # they may share its wording with model-written captions.
                    same = (
                        portuguese == " ".join(draft["pt"].split())
                        and english == " ".join(draft["en"].split())
                        and south == " ".join(draft["pt_south"].split())
                    )
                    record.update(
                        source="model draft",
                        draft_pt=draft["pt"],
                        draft_en=draft["en"],
                        draft_model=draft["model"],
                        verdict="approved" if same else "edited",
                    )
                    if draft["pt_south"]:
                        record["draft_pt_south"] = draft["pt_south"]
            self.ledger.add(record)
            return {"stats": self._stats(), "deleted": False}

    def skip(self, mode: str, pageid: int, query: str) -> dict[str, Any]:
        with self.lock:
            photo = self.photo(pageid)
            key = " ".join(query.casefold().split())
            self.db.execute(
                "INSERT OR IGNORE INTO review_skips (pageid, mode, filter, at)"
                " VALUES (?, ?, ?, ?)",
                (photo.pageid, mode, key, commons.utc_now()),
            )
            self.db.commit()
            self.skips.add((photo.pageid, mode, key))
            return {"ok": True, "stats": self._stats()}

    # -- internals

    def _drafts_left(self) -> tuple[int, int]:
        """Drafts still to correct (all, and those with a southern variant).

        A drafted photo is done once its query is saved or it was skipped in
        the queries mode, under any filter; a draft whose photo is no longer
        downloaded and accepted cannot be corrected and is not counted.
        """
        skipped = {pageid for pageid, mode, _ in self.skips if mode == "queries"}
        left = south = 0
        for pageid, draft in self.drafts.items():
            if pageid not in self.by_id or pageid in skipped:
                continue
            if self.ledger.get(pageid, "query") is not None:
                continue
            left += 1
            south += bool(draft.get("pt_south"))
        return left, south

    def _delete(
        self, base: dict[str, Any], existing: dict[str, Any] | None, message: str
    ) -> dict[str, Any]:
        if existing is None:
            raise BadRequestError(message)
        self.ledger.add({**base, "deleted": True})
        return {"stats": self._stats(), "deleted": True}

    def _series_in_use(self) -> tuple[dict[str, set[int]], set[str]]:
        """Series with evaluation queries (and which photos), and with training text."""
        evaluation: dict[str, set[int]] = {}
        training: set[str] = set()
        for pageid, kind, record in self.ledger.live():
            series = str(record.get("series", ""))
            if kind == "query":
                evaluation.setdefault(series, set()).add(pageid)
            else:
                training.add(series)
        return evaluation, training

    def _conflict(self, mode: str, photo: Photo) -> str | None:
        evaluation, training = self._series_in_use()
        others = evaluation.get(photo.series, set()) - {photo.pageid}
        if mode == "queries":
            if photo.series in training:
                return (
                    "Esta série já tem legenda de treino; não pode ir para a avaliação."
                )
            if others:
                return "Esta série já tem uma foto no conjunto de avaliação."
        elif photo.series in evaluation:
            return (
                "Esta série está no conjunto de avaliação; não pode ir para o treino."
            )
        return None

    def _eligible(self, mode: str, words: list[str]) -> list[Photo]:
        kind = MODES[mode]
        evaluation, training = self._series_in_use()
        key = " ".join(words)
        found: list[Photo] = []
        for photo in self.photos:
            if self.ledger.get(photo.pageid, kind) is not None:
                continue
            if (photo.pageid, mode, key) in self.skips:
                continue
            if words and not all(word in photo.haystack for word in words):
                continue
            if mode == "queries":
                if photo.series in training or photo.series in evaluation:
                    continue
            elif photo.series in evaluation:
                continue
            if mode == "review" and photo.pageid not in self.vlm:
                continue
            found.append(photo)
        if mode == "queries" and self.drafts:
            # Drafted photos first; the sort is stable, so `spread` order holds.
            # Regional pairs first among them, so the set fills before the rest.
            found.sort(
                key=lambda photo: (
                    photo.pageid not in self.drafts,
                    not self.drafts.get(photo.pageid, {}).get("pt_south"),
                )
            )
        return found

    def _empty(self, mode: str, words: list[str]) -> str:
        if not self.photos:
            return (
                "Nenhuma foto baixada ainda. Rode o coletor até a fase download"
                " e reinicie esta página."
            )
        if mode == "review" and not self.vlm:
            return (
                f"Ainda não há legendas do VLM. Esta página as lê de {self.vlm_path}."
            )
        if words:
            return "Nenhuma foto disponível com este filtro neste modo."
        return "Nada mais a fazer neste modo."

    def _item(self, mode: str, photo: Photo, remaining: int) -> dict[str, Any]:
        item: dict[str, Any] = {
            "pageid": photo.pageid,
            "image": f"/image/{photo.pageid}",
            "title": photo.title,
            "description": photo.description,
            "categories": [
                name
                for name in photo.categories
                if not name.casefold().startswith(HOUSEKEEPING)
            ][:12],
            "license": photo.license,
            "author": photo.author,
            "source_url": photo.source_url,
            "series": photo.series,
            "saved": self.ledger.get(photo.pageid, MODES[mode]),
        }
        if mode == "review":
            item["vlm"] = self.vlm.get(photo.pageid)
        if mode == "queries":
            item["draft"] = self.drafts.get(photo.pageid)
        return {"item": item, "remaining": remaining}

    def _stats(self) -> dict[str, Any]:
        focus: Counter[str] = Counter()
        verdicts: Counter[str] = Counter()
        draft_verdicts: Counter[str] = Counter()
        queries = 0
        for _, kind, record in self.ledger.live():
            if kind == "caption":
                focus[str(record.get("focus"))] += 1
            elif kind == "review":
                verdicts[str(record.get("verdict"))] += 1
            elif kind == "query":
                queries += 1
                if record.get("source") == "model draft":
                    draft_verdicts[str(record.get("verdict"))] += 1
        reviewed = sum(verdicts.values())
        wrong = verdicts["edited"] + verdicts["rejected"]
        left, south_left = self._drafts_left()
        return {
            "photos": len(self.photos),
            "captions": sum(focus.values()),
            "focus": dict(focus),
            "vlm": len(self.vlm),
            "reviews": reviewed,
            "review_target": math.ceil(len(self.vlm) * REVIEW_SHARE),
            "verdicts": dict(verdicts),
            "error_rate": wrong / reviewed if reviewed else None,
            "queries": queries,
            "drafts": len(self.drafts),
            "draft_verdicts": dict(draft_verdicts),
            "drafts_left": left,
            "south_left": south_left,
        }


def _pageid(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise BadRequestError("Foto inválida.") from error


def _mode(value: Any) -> str:
    if value not in MODES:
        raise BadRequestError("Modo inválido.")
    return str(value)


def _param(params: Mapping[str, list[str]], name: str) -> str:
    values = params.get(name)
    return values[0] if values else ""


class Handler(BaseHTTPRequestHandler):
    bench: Workbench
    page: bytes
    hosts: frozenset[str]
    origins: frozenset[str]

    def log_message(self, format: str, *args: Any) -> None:
        return  # one line per image would drown the console

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_allowed():
            return
        url = urlparse(self.path)
        params = parse_qs(url.query)
        try:
            if url.path == "/":
                self._send(HTTPStatus.OK, self.page, "text/html; charset=utf-8")
            elif url.path == "/api/next":
                mode = _mode(_param(params, "mode"))
                self._json(self.bench.next(mode, _param(params, "q")))
            elif url.path == "/api/item":
                mode = _mode(_param(params, "mode"))
                self._json(self.bench.item(mode, _pageid(_param(params, "pageid"))))
            elif url.path == "/api/stats":
                self._json(self.bench.stats())
            elif url.path.startswith("/image/"):
                path = self.bench.image_path(_pageid(url.path.removeprefix("/image/")))
                content_type = IMAGE_TYPES.get(path.suffix, "application/octet-stream")
                self._send(HTTPStatus.OK, path.read_bytes(), content_type, cache=True)
            else:
                self._json({"error": "Não encontrado."}, HTTPStatus.NOT_FOUND)
        except BadRequestError as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_allowed():
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.origins:
            self._json({"error": "Origem recusada."}, HTTPStatus.FORBIDDEN)
            return
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self._json({"error": "Envie JSON."}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > 65536:
            self._json(
                {"error": "Pedido grande demais."}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE
            )
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise BadRequestError("Pedido inválido.")
            mode = _mode(payload.get("mode"))
            path = urlparse(self.path).path
            if path == "/api/save":
                self._json(self.bench.save(mode, payload))
            elif path == "/api/skip":
                pageid = _pageid(payload.get("pageid"))
                self._json(self.bench.skip(mode, pageid, str(payload.get("q") or "")))
            else:
                self._json({"error": "Não encontrado."}, HTTPStatus.NOT_FOUND)
        except json.JSONDecodeError:
            self._json({"error": "JSON inválido."}, HTTPStatus.BAD_REQUEST)
        except BadRequestError as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def _host_allowed(self) -> bool:
        if self.headers.get("Host", "") in self.hosts:
            return True
        self._json({"error": "Host recusado."}, HTTPStatus.FORBIDDEN)
        return False

    def _json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send(
        self, status: HTTPStatus, body: bytes, content_type: str, cache: bool = False
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="A local page to write and review captions (RFC-042 4.3, 5.1)."
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="the collector's output directory"
    )
    parser.add_argument(
        "--captions",
        type=Path,
        default=DEFAULT_CAPTIONS,
        help=f"where the work is saved (default: {DEFAULT_CAPTIONS.name} next to"
        " this script, meant to be committed)",
    )
    parser.add_argument(
        "--vlm", type=Path, help=f"VLM captions to review (default: OUT/{VLM_NAME})"
    )
    parser.add_argument(
        "--drafts",
        type=Path,
        help=f"query drafts to correct (default: OUT/{DRAFTS_NAME})",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--no-browser", action="store_true", help="do not open the page in a browser"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_arguments(argv)
    out_dir: Path = args.out
    database = out_dir / commons.DATABASE_NAME
    if not database.is_file():
        raise SystemExit(f"{database} does not exist: run collect_commons.py first")

    # One connection, shared by the server's threads under the workbench lock.
    db = sqlite3.connect(database, timeout=60, check_same_thread=False)
    db.executescript(SKIPS_SCHEMA)
    vlm_path: Path = args.vlm or out_dir / VLM_NAME
    bench = Workbench(
        photos=load_photos(db),
        ledger=Ledger(args.captions),
        vlm=load_vlm(vlm_path),
        vlm_path=vlm_path,
        db=db,
        out_dir=out_dir,
        drafts_path=args.drafts or out_dir / DRAFTS_NAME,
    )

    Handler.bench = bench
    Handler.page = PAGE.read_bytes()
    Handler.hosts = frozenset({f"127.0.0.1:{args.port}", f"localhost:{args.port}"})
    Handler.origins = frozenset(f"http://{host}" for host in Handler.hosts)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as error:
        raise SystemExit(f"port {args.port} is taken ({error}); use --port") from error
    server.daemon_threads = True

    stats = bench.stats()
    url = f"http://127.0.0.1:{args.port}/"
    print(
        f"{stats['photos']} photos; {stats['captions']} captions,"
        f" {stats['reviews']} reviews and {stats['queries']} queries in"
        f" {args.captions}; {stats['vlm']} VLM captions in {vlm_path}"
    )
    print(f"open {url} (Ctrl+C stops)", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
