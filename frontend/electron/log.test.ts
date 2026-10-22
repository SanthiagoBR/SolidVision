import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  consoleLine,
  describeFolders,
  describeMap,
  describeSearch,
  failedLoadLine,
  logDirectory,
  redactUrl,
  redactUrls,
  rotate,
  ShellLog,
  timestamp,
} from "./log";

let work: string;

beforeEach(() => {
  work = fs.mkdtempSync(path.join(os.tmpdir(), "shell-log-"));
});

afterEach(() => {
  fs.rmSync(work, { recursive: true, force: true });
});

describe("logDirectory", () => {
  it("is logs under the data root of the mode", () => {
    const local = path.join(work, "Local");

    expect(logDirectory(false, { LOCALAPPDATA: local })).toBe(
      path.join(local, "SolidVision-dev", "logs"),
    );
    expect(logDirectory(true, { LOCALAPPDATA: local })).toBe(
      path.join(local, "SolidVision", "logs"),
    );
  });

  it("ignores a LOCALAPPDATA that is not absolute, as the backend does", () => {
    expect(logDirectory(true, { LOCALAPPDATA: "relative" })).toBe(
      path.join(os.homedir(), "AppData", "Local", "SolidVision", "logs"),
    );
  });
});

describe("rotate", () => {
  it("moves a log above the limit to .1, replacing the last one", () => {
    const file = path.join(work, "shell.log");
    fs.writeFileSync(file, "x".repeat(11));
    fs.writeFileSync(`${file}.1`, "older");

    expect(rotate(file, 10)).toBe(true);
    expect(fs.existsSync(file)).toBe(false);
    expect(fs.readFileSync(`${file}.1`, "utf8")).toBe("x".repeat(11));
  });

  it("leaves a log at the limit, or none at all, alone", () => {
    const file = path.join(work, "shell.log");

    expect(rotate(file, 10)).toBe(false);
    fs.writeFileSync(file, "x".repeat(10));
    expect(rotate(file, 10)).toBe(false);
    expect(fs.readFileSync(file, "utf8")).toBe("x".repeat(10));
  });
});

describe("ShellLog", () => {
  it("writes the launcher's lines as they come, its own with a time, and the marks", () => {
    const directory = path.join(work, "logs");
    const log = ShellLog.open(directory);
    log.raw("api | 2026-10-20 10:00:00 | INFO | app | Started");
    log.info("event {}");
    log.mark("window", 812.4, "x");
    log.close();
    log.info("after the close, nothing");

    const lines = fs
      .readFileSync(path.join(directory, "shell.log"), "utf8")
      .split(os.EOL);
    expect(lines[0]).toBe("api | 2026-10-20 10:00:00 | INFO | app | Started");
    expect(lines[1]).toMatch(
      /^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} \| INFO \| shell \| event \{\}$/,
    );
    expect(lines[2]).toMatch(/\| INFO \| shell \| mark window \+812 ms x$/);
    expect(lines).toHaveLength(4);
  });

  it("a log that cannot be opened does not stop the window", () => {
    const blocked = path.join(work, "a-file");
    fs.writeFileSync(blocked, "");

    const log = ShellLog.open(path.join(blocked, "logs"));
    expect(() => log.info("nowhere")).not.toThrow();
  });
});

it("writes the time as the backend's formatter does, with the milliseconds", () => {
  expect(timestamp(new Date(2026, 9, 20, 9, 5, 7, 42))).toBe("2026-10-20 09:05:07.042");
});

describe("no query string reaches shell.log (RFC-038 section 2.7)", () => {
  const search =
    "http://127.0.0.1:52011/busca?q=telhado%20azul&perto=-26.32141,-48.81631&raio=1000";

  it("cuts a URL at its query string or its fragment", () => {
    expect(redactUrl(search)).toBe("http://127.0.0.1:52011/busca?…");
    expect(redactUrl("http://127.0.0.1:52011/busca#q=x")).toBe(
      "http://127.0.0.1:52011/busca#…",
    );
    expect(redactUrl("http://127.0.0.1:52011/")).toBe("http://127.0.0.1:52011/");
    expect(redactUrl("")).toBe("");
  });

  it("cuts every URL inside a text", () => {
    const text = `GET ${search} 503 (Service Unavailable); see ${search}`;

    expect(redactUrls(text)).toBe(
      "GET http://127.0.0.1:52011/busca?… 503 (Service Unavailable); see " +
        "http://127.0.0.1:52011/busca?…",
    );
  });

  it("the line of did-fail-load goes through it", () => {
    const line = failedLoadLine("ERR_CONNECTION_REFUSED", -102, search);

    expect(line).toBe(
      "The page did not load: ERR_CONNECTION_REFUSED (-102) at " +
        "http://127.0.0.1:52011/busca?…",
    );
    expect(line).not.toContain("telhado");
    expect(line).not.toContain("-26.32141");
  });

  it("the line of console-message goes through it, sourceId and message", () => {
    const api = "http://127.0.0.1:52011/api/v1/images/search?q=telhado+azul&limit=40";
    const line = consoleLine("error", `Failed to load resource: ${api}`, api, 0);

    expect(line).not.toContain("telhado");
    expect(line).toContain("/api/v1/images/search?…");
  });
});

describe("no area of the map reaches shell.log (RFC-040 section 2.7)", () => {
  it("cuts the path of a tile, which is a place", () => {
    expect(redactUrl("https://tile.openstreetmap.org/12/1492/2358.png")).toBe(
      "https://tile.openstreetmap.org/…",
    );
    expect(redactUrl("http://example.com:8080/a/b?c=d#e")).toBe(
      "http://example.com:8080/…",
    );
    expect(redactUrl("https://user:secret@example.com/x")).toBe(
      "https://example.com/…",
    );
  });

  it("keeps the path of the origin's own URLs, without the query, as before", () => {
    expect(
      redactUrl("http://127.0.0.1:52011/mapa?centro=-26.3214,-48.8163&zoom=9"),
    ).toBe("http://127.0.0.1:52011/mapa?…");
    expect(redactUrl("http://localhost:5173/api/v1/images/map?min_lat=-27")).toBe(
      "http://localhost:5173/api/v1/images/map?…",
    );
    expect(redactUrl("http://127.0.0.1:52011/")).toBe("http://127.0.0.1:52011/");
  });

  it("keeps a file: URL as it was: the start page", () => {
    const start = "file:///C:/Users/x/SolidVision/frontend/electron/startup.html";

    expect(redactUrl(start)).toBe(start);
    expect(redactUrl(`${start}?a=b`)).toBe(`${start}?…`);
  });

  it("leaves no tile path in a line of the console", () => {
    const tile = "https://tile.openstreetmap.org/12/1492/2358.png";
    const line = consoleLine(
      "error",
      `Failed to load resource: net::ERR_NAME_NOT_RESOLVED ${tile}`,
      tile,
      0,
    );

    expect(line).not.toMatch(/tile\.openstreetmap\.org\/\d/);
    expect(line).toContain("https://tile.openstreetmap.org/…");
  });

  it("the host alone is kept as it is", () => {
    expect(redactUrl("https://tile.openstreetmap.org")).toBe(
      "https://tile.openstreetmap.org",
    );
    expect(redactUrl("https://tile.openstreetmap.org/")).toBe(
      "https://tile.openstreetmap.org/",
    );
  });
});

describe("the mark of the map (RFC-040 section 5.6)", () => {
  it("carries the counts, the precisions and the times, and no coordinate", () => {
    const detail = describeMap({
      cells: 24,
      precisionRequested: 1,
      precisionApplied: 0,
      responseMs: 412.4,
      paintedMs: 530.6,
      box: { south: -26.5, west: -49, north: -26.1, east: -48.6 },
      centre: "-26.3214,-48.8163",
    });

    expect(detail).toBe("cells=24 precision=1->0 response=412 painted=531");
    expect(detail).not.toContain("-26");
    expect(detail).not.toContain("48.8");
  });

  it("does not trust what it is sent", () => {
    expect(describeMap({ cells: "muitas" })).toBe(
      "cells=? precision=?->? response=? painted=?",
    );
    expect(describeMap(null)).toBe("");
  });
});

describe("the mark of a search", () => {
  it("carries the kind and the counts, and nothing else", () => {
    const detail = describeSearch({
      kind: "text",
      limit: 40,
      results: 40,
      responseMs: 412.4,
      paintedMs: 530.6,
      q: "telhado azul",
    });

    expect(detail).toBe("kind=text limit=40 results=40 response=412 painted=531");
    expect(detail).not.toContain("telhado");
  });

  it("does not trust what it is sent", () => {
    expect(describeSearch({ kind: "telhado azul", limit: "x" })).toBe(
      "kind=? limit=? results=? response=? painted=?",
    );
    expect(describeSearch(null)).toBe("");
  });
});

describe("the mark of a listing of folders (RFC-039 section 5.9)", () => {
  it("carries the count and the times, and never the path", () => {
    const detail = describeFolders({
      folders: 5000,
      responseMs: 947.2,
      paintedMs: 1210.7,
      path: "Fotos/casamento da Ana",
    });

    expect(detail).toBe("folders=5000 response=947 painted=1211");
    expect(detail).not.toContain("casamento");
  });

  it("does not trust what it is sent", () => {
    expect(describeFolders({ folders: "muitas" })).toBe(
      "folders=? response=? painted=?",
    );
    expect(describeFolders(undefined)).toBe("");
  });
});
