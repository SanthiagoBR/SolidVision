/**
 * The window's main process (RFC-037 section 6).
 *
 * In order: the name, before the lock -- `SolidVision` packaged and
 * `SolidVision-dev` in development, each with its own lock and its own
 * Chromium profile, side by side as the data roots of RFC-036 section 8 are;
 * the lock, and a second window that leaves at once and brings the first to
 * the front; the launcher, before the window, because the backend is the long
 * part; and one window, shown with the start page, that goes to the URL of
 * `ready`.
 *
 * What each event does is `events.ts`. This file carries the actions out: the
 * pages, the bridge, the timers, the launcher, the close of section 6.4, and
 * the marks of section 10 in `shell.log`.
 */

import {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  Menu,
  session,
  shell,
  type IpcMainEvent,
  type IpcMainInvokeEvent,
} from "electron";
import * as fs from "node:fs";
import * as path from "node:path";
import { performance } from "node:perf_hooks";

import {
  INITIAL,
  PRODUCTION,
  parseLine,
  step,
  type Action,
  type Input,
  type Screen,
  type ShellOptions,
  type ShellState,
} from "./events";
import { LauncherProcess } from "./launcher";
import {
  consoleLine,
  describeSearch,
  failedLoadLine,
  logDirectory,
  redactUrl,
  ShellLog,
} from "./log";
import { resolvePython } from "./python";

/** The launcher's own deadlines (RFC-036 sections 7.2 and 16): 30 s for the job
 * runner, beside the API's 15 s, and the cluster's `stop()`, measured from 0.8
 * to 9.2 s. The 60 is a choice. */
const STOP_DEADLINE_MS = 60_000;

const REQUEST_TIMEOUT_MS = 2_000;
const RELAUNCH_RETRY_MS = 200;
/** `did-fail-load` of a load that another one replaced: not a failure. */
const ERR_ABORTED = -3;

const NAME = app.isPackaged ? "SolidVision" : "SolidVision-dev";
app.setName(NAME);
app.setPath("userData", path.join(app.getPath("appData"), NAME));

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  main();
}

function main(): void {
  const packaged = app.isPackaged;
  const logs = logDirectory(packaged, process.env);
  const log = ShellLog.open(logs);
  const since = () => performance.now();
  log.info(
    `SolidVision ${app.getVersion()} (${NAME}), Electron ${process.versions.electron}, ` +
      `PID ${process.pid}`,
  );
  log.mark("main", since());

  // `npm run dev` only: the page from Vite, whose proxy sends /api to API_PORT.
  const devServerUrl = packaged ? null : process.env.SOLIDVISION_DEV_SERVER_URL || null;
  const options: ShellOptions =
    devServerUrl === null
      ? PRODUCTION
      : { devServerUrl, devApiPort: Number(process.env.API_PORT ?? "8000") };
  const startPage = path.join(app.getAppPath(), "electron", "startup.html");
  const repositoryRoot = path.dirname(app.getAppPath());

  let state: ShellState = INITIAL;
  let window: BrowserWindow | null = null;
  let launcher: LauncherProcess | null = null;
  let stopTimer: NodeJS.Timeout | null = null;
  let screen: Screen = state.screen;
  let onStartPage = false;
  let pageUrl: string | null = null;
  let closeRequested = false;
  let quitting = false;
  let readyMarked = false;
  let homeMarked = false;

  function feed(input: Input): void {
    const result = step(state, input, performance.now(), options);
    state = result.state;
    for (const action of result.actions) {
      perform(action);
    }
  }

  function perform(action: Action): void {
    switch (action.kind) {
      case "show":
        showScreen(action.screen);
        return;
      case "open":
        openPage(action.url);
        return;
      case "forward":
        if (window !== null && !onStartPage) {
          window.webContents.send("launcher-event", action.event);
        }
        return;
      case "launch":
        setTimeout(startLauncher, action.afterMs);
        return;
      case "probe":
        setTimeout(() => void probe(action.url), action.afterMs);
        return;
      case "stop":
        stopLauncher();
        return;
      case "quit":
        quit();
        return;
    }
  }

  // -- the launcher ---------------------------------------------------------

  function startLauncher(): void {
    if (quitting) {
      return;
    }
    if (launcher !== null && launcher.alive) {
      // The last one has not finished leaving: a second launcher says where
      // the first is and exits at once.
      setTimeout(startLauncher, RELAUNCH_RETRY_MS);
      return;
    }
    const resolution = resolvePython({
      packaged,
      repositoryRoot,
      environment: process.env,
      exists: (file) => fs.existsSync(file),
    });
    if (!resolution.ok) {
      log.warning(resolution.line);
      feed({ kind: "not-launched", detail: resolution.line });
      return;
    }
    const { command } = resolution;
    log.info(
      `Starting ${command.executable} ${command.args.join(" ")} in ${command.cwd}`,
    );
    let started: LauncherProcess;
    try {
      started = LauncherProcess.start(command, {
        stdout: onLine,
        stderr: (line) => log.raw(line),
        exit: (code, lines) => {
          if (started !== launcher) {
            return;
          }
          log.info(`The launcher exited with ${code ?? "no code"}`);
          if (stopTimer !== null) {
            clearTimeout(stopTimer);
            stopTimer = null;
          }
          feed({ kind: "exited", code, lines });
        },
        failed: (error) => {
          log.warning(`The launcher could not be started: ${error.message}`);
          feed({
            kind: "not-launched",
            detail: `O launcher não pôde ser iniciado com ${command.executable}: ${error.message}`,
          });
        },
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      log.warning(`The launcher could not be started: ${message}`);
      feed({ kind: "not-launched", detail: message });
      return;
    }
    launcher = started;
    log.mark("launcher", since(), `PID ${started.pid ?? "?"}`);
    feed({ kind: "launched" });
  }

  function onLine(line: string): void {
    const parsed = parseLine(line);
    if (parsed.kind === "anomaly") {
      log.warning(
        `A line on the launcher's stdout that is not an event (${parsed.reason}): ${line}`,
      );
      return;
    }
    log.info(`event ${line}`);
    if (parsed.event.event === "ready" && !readyMarked) {
      readyMarked = true;
      log.mark("ready", since(), parsed.event.url);
    }
    if (parsed.event.event === "stopped") {
      log.mark("stopped", since());
    }
    feed({ kind: "event", event: parsed.event });
  }

  function stopLauncher(): void {
    const current = launcher;
    if (current === null || !current.alive) {
      return;
    }
    log.info("Asking the launcher to stop: its stdin is closed");
    current.requestStop();
    stopTimer = setTimeout(() => {
      if (current.alive) {
        log.warning(
          `The launcher did not stop in ${STOP_DEADLINE_MS / 1000} s, and was ended ` +
            "(TerminateProcess). Its children stop at the end of their stdin, and the " +
            "database is left for the next start to adopt (RFC-036 section 7.4).",
        );
        current.kill();
      }
    }, STOP_DEADLINE_MS);
  }

  // -- the pages ------------------------------------------------------------

  function showScreen(next: Screen): void {
    screen = next;
    if (window === null) {
      return;
    }
    if (onStartPage) {
      window.webContents.send("screen", next);
      return;
    }
    onStartPage = true;
    pageUrl = null;
    void window.loadFile(startPage);
  }

  function openPage(url: string): void {
    onStartPage = false;
    pageUrl = url;
    if (window === null) {
      return;
    }
    // A failure arrives as `did-fail-load`, and is handled there.
    window.loadURL(url).catch(() => undefined);
  }

  async function probe(url: string): Promise<void> {
    if (quitting) {
      return;
    }
    let answered: boolean;
    try {
      const response = await fetch(url, {
        signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      });
      answered = response.status < 500;
    } catch {
      answered = false;
    }
    feed({ kind: "probed", answered });
  }

  function isOurPage(target: string): boolean {
    if (pageUrl === null) {
      return false;
    }
    try {
      return new URL(target).origin === new URL(pageUrl).origin;
    } catch {
      return false;
    }
  }

  function createWindow(): BrowserWindow {
    const created = new BrowserWindow({
      width: 1280,
      height: 800,
      minWidth: 960,
      minHeight: 600,
      show: false,
      backgroundColor: "#ffffff",
      title: "SolidVision",
      autoHideMenuBar: true,
      webPreferences: {
        preload: path.join(__dirname, "preload.js"),
        contextIsolation: true,
        sandbox: true,
        nodeIntegration: false,
        spellcheck: false,
      },
    });
    created.once("ready-to-show", () => {
      created.show();
      log.mark("window", since());
    });
    created.on("close", (event) => {
      if (quitting) {
        return;
      }
      event.preventDefault();
      void requestClose();
    });
    const contents = created.webContents;
    contents.setWindowOpenHandler(() => ({ action: "deny" }));
    contents.on("will-navigate", (event, target) => {
      if (!isOurPage(target)) {
        event.preventDefault();
        log.warning(`A navigation to ${redactUrl(target)} was blocked`);
      }
    });
    contents.on("did-navigate", (_event, url, code) => {
      if (!onStartPage && code === 404 && isOurPage(url)) {
        log.warning(
          `The page answered 404 at ${redactUrl(url)}: the API serves no interface`,
        );
        feed({ kind: "no-interface" });
      }
    });
    contents.on("did-finish-load", () => {
      if (!onStartPage) {
        log.mark("page", since(), redactUrl(pageUrl ?? ""));
      }
    });
    contents.on("did-fail-load", (_event, code, description, url, isMainFrame) => {
      if (!isMainFrame || code === ERR_ABORTED || onStartPage) {
        return;
      }
      log.warning(failedLoadLine(description, code, url));
      feed({ kind: "load-failed" });
    });
    // What the page says when something fails -- a request refused, an
    // exception -- in the log a user can send, rather than in a console no one
    // opens. Every URL in it without its query string: the search is there
    // (RFC-038 section 2.7).
    contents.on("console-message", (details) => {
      if (details.level === "warning" || details.level === "error") {
        log.warning(
          consoleLine(
            details.level,
            details.message,
            details.sourceId,
            details.lineNumber,
          ),
        );
      }
    });
    // A page whose process is gone is a white window: it is loaded again, the
    // way a page that lost its origin is.
    contents.on("render-process-gone", (_event, details) => {
      log.warning(
        `The page's process is gone: ${details.reason} (${details.exitCode})`,
      );
      if (!onStartPage) {
        feed({ kind: "load-failed" });
      }
    });
    if (pageUrl !== null) {
      // `ready` came before the window existed.
      openPage(pageUrl);
    } else {
      onStartPage = true;
      void created.loadFile(startPage);
    }
    return created;
  }

  // -- the bridge -----------------------------------------------------------

  function fromOurWindow(event: IpcMainEvent | IpcMainInvokeEvent): boolean {
    return window !== null && event.sender === window.webContents;
  }

  ipcMain.handle("screen", (event) => (fromOurWindow(event) ? screen : null));
  ipcMain.on("open-logs-folder", (event) => {
    if (fromOurWindow(event)) {
      void shell.openPath(logs);
    }
  });
  ipcMain.on("reopen", (event) => {
    if (fromOurWindow(event)) {
      log.info("Opening again, as asked");
      feed({ kind: "reopen" });
    }
  });
  ipcMain.on("home-ready", (event, report: unknown) => {
    if (fromOurWindow(event) && !homeMarked) {
      homeMarked = true;
      log.mark("home", since(), describeHome(report));
    }
  });
  // One per search, unlike the home's: the numbers of RFC-038 section 8.
  ipcMain.on("search-done", (event, report: unknown) => {
    if (fromOurWindow(event)) {
      log.mark("search", since(), describeSearch(report));
    }
  });

  // -- closing (section 6.4) -------------------------------------------------

  async function requestClose(): Promise<void> {
    if (closeRequested) {
      return;
    }
    closeRequested = true;
    log.mark("close", since());
    if (
      state.owner &&
      state.stage === "open" &&
      state.url !== null &&
      window !== null
    ) {
      const running = await runningJobs(state.url);
      if (running > 0) {
        const { response } = await dialog.showMessageBox(window, {
          type: "question",
          buttons: ["Fechar", "Continuar aberto"],
          defaultId: 1,
          cancelId: 1,
          noLink: true,
          title: "SolidVision",
          message: "Há uma indexação em andamento.",
          detail: "A indexação pausa e continua na próxima abertura.",
        });
        if (response !== 0) {
          log.info("The close was cancelled: an indexing job is running");
          closeRequested = false;
          return;
        }
      }
    }
    feed({ kind: "close" });
  }

  async function runningJobs(url: string): Promise<number> {
    try {
      const response = await fetch(`${url}/api/v1/jobs?status=running`, {
        signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      });
      const body = (await response.json()) as { jobs?: unknown[] };
      return Array.isArray(body.jobs) ? body.jobs.length : 0;
    } catch (error) {
      log.warning(`The running jobs could not be read: ${String(error)}`);
      return 0;
    }
  }

  function quit(): void {
    if (quitting) {
      return;
    }
    quitting = true;
    log.mark("quit", since());
    log.close();
    app.quit();
  }

  // -- the life of the app ----------------------------------------------------

  app.on("second-instance", () => {
    log.info("A second opening brought this window to the front");
    if (window !== null) {
      if (window.isMinimized()) {
        window.restore();
      }
      window.show();
      window.focus();
    }
  });
  app.on("window-all-closed", () => app.quit());

  startLauncher();

  void app.whenReady().then(() => {
    if (packaged) {
      Menu.setApplicationMenu(null);
    }
    // No camera, no microphone, no notifications. Writing to the clipboard is
    // RFC-038's "Copiar caminho".
    session.defaultSession.setPermissionRequestHandler(
      (_contents, permission, callback) =>
        callback(permission === "clipboard-sanitized-write"),
    );
    window = createWindow();
  });
}

function describeHome(report: unknown): string {
  if (typeof report !== "object" || report === null) {
    return "";
  }
  const fields = report as Record<string, unknown>;
  const number = (value: unknown) => (typeof value === "number" ? value : "?");
  const last = typeof fields.lastIndexedAt === "string" ? fields.lastIndexedAt : "none";
  return (
    `devices=${number(fields.devices)} indexed=${number(fields.indexed)} ` +
    `last=${JSON.stringify(last)}`
  );
}
