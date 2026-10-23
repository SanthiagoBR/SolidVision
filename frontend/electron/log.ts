/**
 * The window's log, `shell.log` (RFC-037 section 6.5).
 *
 * In `logs` under the data root of the mode: `%LOCALAPPDATA%\SolidVision\logs`
 * packaged, `SolidVision-dev\logs` in development (RFC-034 section 6.2). The
 * window does not read the configuration: a `LOG_DIRECTORY` changed in the
 * user's file does not move this one.
 *
 * What goes in: the launcher's stderr as it arrives -- every child's lines pass
 * through it --, the window's own lines, and the marks of each opening, which
 * are the numbers of section 10: `mark <name> +<ms> ms`, the milliseconds since
 * the process began, after the clock time of the line.
 *
 * Above 10 MB at the opening it becomes `shell.log.1`, the limit of the
 * backend's logs. One process writes it, so RFC-034's rotation is not needed.
 * A log that cannot be written never stops the window.
 */

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

export const LOG_NAME = "shell.log";
export const ROTATE_BYTES = 10 * 1024 * 1024;
export const INSTALLED_DATA_NAME = "SolidVision";
export const DEVELOPMENT_DATA_NAME = "SolidVision-dev";

/** `logs` under the data root of the mode, by the backend's rule: the variable,
 * when it is an absolute path, and the profile's folder otherwise. */
export function logDirectory(
  packaged: boolean,
  environment: Readonly<Record<string, string | undefined>>,
): string {
  const variable = environment.LOCALAPPDATA;
  const local =
    variable && path.isAbsolute(variable)
      ? variable
      : path.join(os.homedir(), "AppData", "Local");
  return path.join(
    local,
    packaged ? INSTALLED_DATA_NAME : DEVELOPMENT_DATA_NAME,
    "logs",
  );
}

/** `shell.log` to `shell.log.1`, replacing it, when above the limit. */
export function rotate(file: string, limit: number = ROTATE_BYTES): boolean {
  let size: number;
  try {
    size = fs.statSync(file).size;
  } catch {
    return false;
  }
  if (size <= limit) {
    return false;
  }
  fs.rmSync(`${file}.1`, { force: true });
  fs.renameSync(file, `${file}.1`);
  return true;
}

/**
 * A URL as it may go into `shell.log`: without its query string or fragment
 * (RFC-038 section 2.7), and, from another host, without its path (RFC-040
 * section 2.7).
 *
 * The search lives in the URL of the screen -- `/busca?q=…`, `perto=lat,lon`
 * -- and RFC-026 section 16.1 decided the text of a query goes to no log. So
 * everything from the first `?` or `#` is cut, of every URL this file writes:
 * one function, so that no line can forget it.
 *
 * **A map tile carries the place in its path**: `/12/1492/2358.png` is a
 * square of ~9 km, and the area one looks at is the map's query. So an `http`
 * or `https` URL of a host that is neither `127.0.0.1` nor `localhost` keeps
 * its scheme and host only -- `https://tile.openstreetmap.org/…` --, and the
 * origin's own keep their path, as before. A `file:` URL is the start page's.
 */
export function redactUrl(url: string): string {
  const outside = /^(https?:\/\/)(?:[^/?#@]*@)?([^/?#]*)/i.exec(url);
  if (outside !== null) {
    const [authority, scheme = "", hostAndPort = ""] = outside;
    const host = hostAndPort.replace(/:\d*$/, "").toLowerCase();
    if (host !== "127.0.0.1" && host !== "localhost") {
      const rest = url.slice(authority.length);
      return rest === "" || rest === "/" ? url : `${scheme}${hostAndPort}/…`;
    }
  }
  const cut = url.search(/[?#]/);
  return cut < 0 ? url : `${url.slice(0, cut)}${url[cut]}…`;
}

const URL_IN_TEXT = /\b[a-z][a-z0-9+.-]*:\/\/[^\s"'<>()]+/gi;

/** Every URL inside a text -- a console message, an error -- redacted. */
export function redactUrls(text: string): string {
  return text.replace(URL_IN_TEXT, (url) => redactUrl(url));
}

/** The line of a page that did not load: the URL redacted. */
export function failedLoadLine(description: string, code: number, url: string): string {
  return `The page did not load: ${description} (${code}) at ${redactUrl(url)}`;
}

/** The line of a warning or an error of the page's console: the message and
 * its source redacted -- a failed request names its URL, query and all. */
export function consoleLine(
  level: string,
  message: string,
  sourceId: string,
  lineNumber: number,
): string {
  return (
    `The page's console, ${level}: ${redactUrls(message)} ` +
    `(${redactUrl(sourceId)}:${lineNumber})`
  );
}

/** `kind=text limit=40 results=40 response=412 painted=530`: the kind and the
 * counts, never the query. */
export function describeSearch(report: unknown): string {
  if (typeof report !== "object" || report === null) {
    return "";
  }
  const fields = report as Record<string, unknown>;
  const kind =
    fields.kind === "text" ||
    fields.kind === "similar" ||
    fields.kind === "image" ||
    fields.kind === "browse"
      ? fields.kind
      : "?";
  const number = (value: unknown) =>
    typeof value === "number" && Number.isFinite(value) ? Math.round(value) : "?";
  return (
    `kind=${kind} limit=${number(fields.limit)} results=${number(fields.results)} ` +
    `response=${number(fields.responseMs)} painted=${number(fields.paintedMs)}`
  );
}

/** `folders=5000 response=947 painted=1210`: one listing of a device's
 * folders, painted (RFC-039 section 5.9). The counts and the times, never the
 * path, which is the user's. */
export function describeFolders(report: unknown): string {
  if (typeof report !== "object" || report === null) {
    return "";
  }
  const fields = report as Record<string, unknown>;
  const number = (value: unknown) =>
    typeof value === "number" && Number.isFinite(value) ? Math.round(value) : "?";
  return (
    `folders=${number(fields.folders)} response=${number(fields.responseMs)} ` +
    `painted=${number(fields.paintedMs)}`
  );
}

/** `cells=24 precision=3->3 response=120 painted=180`: one drawing of the map
 * (RFC-040 section 5.6). The counts, the precisions and the times, never the
 * box nor the framing: the area one looks at is the map's query. */
export function describeMap(report: unknown): string {
  if (typeof report !== "object" || report === null) {
    return "";
  }
  const fields = report as Record<string, unknown>;
  const number = (value: unknown) =>
    typeof value === "number" && Number.isFinite(value) ? Math.round(value) : "?";
  return (
    `cells=${number(fields.cells)} precision=${number(fields.precisionRequested)}->` +
    `${number(fields.precisionApplied)} response=${number(fields.responseMs)} ` +
    `painted=${number(fields.paintedMs)}`
  );
}

/** The clock time of a line, as the backend's formatter writes it, with the
 * milliseconds. */
export function timestamp(date: Date = new Date()): string {
  const pad = (value: number, width = 2) => String(value).padStart(width, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}` +
    `.${pad(date.getMilliseconds(), 3)}`
  );
}

export class ShellLog {
  readonly file: string;
  private descriptor: number | null;

  private constructor(file: string, descriptor: number | null) {
    this.file = file;
    this.descriptor = descriptor;
  }

  static open(directory: string): ShellLog {
    const file = path.join(directory, LOG_NAME);
    try {
      fs.mkdirSync(directory, { recursive: true });
      rotate(file);
      return new ShellLog(file, fs.openSync(file, "a"));
    } catch {
      return new ShellLog(file, null);
    }
  }

  /** A line of the launcher's stderr, as it arrived. */
  raw(line: string): void {
    this.write(line);
  }

  info(message: string): void {
    this.write(`${timestamp()} | INFO | shell | ${message}`);
  }

  warning(message: string): void {
    this.write(`${timestamp()} | WARNING | shell | ${message}`);
  }

  /** A mark of this opening (section 10). */
  mark(name: string, sinceStartMs: number, detail?: string): void {
    const suffix = detail ? ` ${detail}` : "";
    this.info(`mark ${name} +${Math.round(sinceStartMs)} ms${suffix}`);
  }

  /** Nothing is written after this: a descriptor number the system hands out
   * again would otherwise receive the lines meant for this file. */
  close(): void {
    const descriptor = this.descriptor;
    this.descriptor = null;
    if (descriptor !== null) {
      try {
        fs.closeSync(descriptor);
      } catch {
        // Already closed.
      }
    }
  }

  private write(line: string): void {
    if (this.descriptor === null) {
      return;
    }
    try {
      // Synchronous, one line at a time: a window that dies keeps every line
      // it wrote before, which is when this log is read.
      fs.writeSync(this.descriptor, line + os.EOL);
    } catch {
      // A full disk does not take the window down.
    }
  }
}
