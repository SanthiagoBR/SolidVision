import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { logDirectory, rotate, ShellLog, timestamp } from "./log";

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
