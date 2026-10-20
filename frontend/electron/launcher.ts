/**
 * The launcher, as a child of the window (RFC-037 sections 6.2 and 6.5).
 *
 *     spawn(<python>, ["-m", "app.launcher", "--supervised"],
 *           { windowsHide: true, stdio: ["pipe", "pipe", "pipe"] })
 *
 * **All three handles are pipes** (section 2.7). A file in `stdio` is a handle
 * the child inherits, and libuv adds `CREATE_NO_WINDOW` only when no handle is
 * inherited: with its stderr on a file, the launcher would get a console window
 * created hidden, instead of none. The window writes the stderr to `shell.log`
 * itself, and keeps the last 50 lines for the error screen -- the launcher
 * keeps as many of each child (RFC-036 section 5.5).
 *
 * stdout is read a line at a time, and each line is an event. stderr is read
 * always: a pipe nobody empties blocks the launcher on its next write (RFC-036
 * section 4.4). stdin is never written. Closing it is the request to stop
 * (RFC-036 section 7.1), and when the window dies Windows closes it, and the
 * request arrives just the same (section 7.4).
 */

import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";

import type { LauncherCommand } from "./python";

export const TAIL_LINES = 50;
/** How long the readers of a launcher that exited get to deliver its last
 * lines -- the `stopped` among them -- before the exit is reported. */
export const EXIT_DRAIN_MS = 2_000;

export interface LauncherListener {
  /** A line of stdout: an event, or an anomaly. */
  stdout(line: string): void;
  stderr(line: string): void;
  /** Once, after the last line of both streams, or after the drain. */
  exit(code: number | null, lines: readonly string[]): void;
  /** The process could not be created. `exit` is not called then. */
  failed(error: Error): void;
}

export class LauncherProcess {
  private readonly child: ChildProcessWithoutNullStreams;
  private readonly tail: string[] = [];
  private reported = false;

  private constructor(
    child: ChildProcessWithoutNullStreams,
    listener: LauncherListener,
  ) {
    this.child = child;
    let open = 2;
    let exited = false;
    let code: number | null = null;
    const report = () => {
      if (this.reported || !exited) {
        return;
      }
      this.reported = true;
      listener.exit(code, [...this.tail]);
    };
    const ended = () => {
      open -= 1;
      if (open === 0) {
        report();
      }
    };
    createInterface({ input: child.stdout, crlfDelay: Infinity })
      .on("line", (line) => listener.stdout(line))
      .on("close", ended);
    createInterface({ input: child.stderr, crlfDelay: Infinity })
      .on("line", (line) => {
        this.tail.push(line);
        if (this.tail.length > TAIL_LINES) {
          this.tail.shift();
        }
        listener.stderr(line);
      })
      .on("close", ended);
    child.on("exit", (exitCode) => {
      exited = true;
      code = exitCode;
      if (open === 0) {
        report();
      } else {
        // A process the launcher left behind could hold a copy of a pipe: the
        // exit is not held hostage by it.
        setTimeout(report, EXIT_DRAIN_MS).unref();
      }
    });
    child.on("error", (error) => {
      if (child.pid === undefined && !this.reported) {
        this.reported = true;
        listener.failed(error);
      }
    });
    // Closing stdin after the launcher has gone is not an error of the window.
    child.stdin.on("error", () => undefined);
  }

  static start(command: LauncherCommand, listener: LauncherListener): LauncherProcess {
    const child = spawn(command.executable, [...command.args], {
      cwd: command.cwd,
      env: command.env,
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    return new LauncherProcess(child, listener);
  }

  get pid(): number | undefined {
    return this.child.pid;
  }

  /** Until its exit has been reported. */
  get alive(): boolean {
    return !this.reported;
  }

  lastLines(): readonly string[] {
    return [...this.tail];
  }

  /** Close its stdin: the request to stop. */
  requestStop(): void {
    if (!this.child.stdin.destroyed) {
      this.child.stdin.end();
    }
  }

  /** `TerminateProcess`, past the deadline of the stop (section 6.4). */
  kill(): void {
    if (this.child.exitCode === null && this.child.signalCode === null) {
      this.child.kill();
    }
  }
}
