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
