/**
 * The launcher's events, and what the window does with each (RFC-037 section 6.3).
 *
 * No Electron here. `step` takes the state, one input and the time, and gives
 * the next state and the actions; `main.ts` carries the actions out. The tests
 * run every row of the table of section 6.3 through it, and the exit without a
 * `stopped`, which no event announces.
 *
 *     event                     the window
 *     starting                  `protocol` 1, or an error: versions that do
 *                               not understand each other
 *     phase                     the text of the start screen
 *     ready                     the page, at the `url`; the window owns the
 *                               launcher. In `npm run dev`, Vite's page
 *     already_running, ready    the page, at the `url`, without owning it
 *     already_running, other    the launcher again in 1 s, for up to 120 s
 *     failed                    the error, and "Abrir a pasta de logs"
 *     child                     to the page; `api` `restarting` is
 *                               "Reconectando…", and `running` reloads it
 *     address                   the page, at the new `url`
 *     stopping, stopped         "Encerrando…", and the window quits
 *     an exit, before ready     the error, with the last lines of stderr
 *     an exit, after            "O SolidVision parou", and "Abrir de novo"
 */

/** The launcher's protocol this window speaks (RFC-036 section 4.4). */
export const PROTOCOL = 1;

export const RELAUNCH_DELAY_MS = 1_000;
/** The launcher's own deadline to be ready (RFC-036 section 17.2). */
export const WAIT_FOR_ANOTHER_MS = 120_000;

export const PROBE_DELAY_MS = 1_000;
/** Reconnecting this long to a launcher this window does not own, and it
 * starts one: that one either becomes the owner or says where the other is. */
export const ADOPT_AFTER_MS = 30_000;

/** The events of RFC-036 section 4.4. The fields this window reads are checked
 * by `parseLine`; the others are typed as the launcher writes them. */
export type LauncherEvent =
  | {
      readonly event: "starting";
      readonly protocol: number;
      readonly pid?: number;
      readonly mode?: string;
      readonly database?: string;
    }
  | { readonly event: "phase"; readonly phase: string }
  | {
      readonly event: "ready";
      readonly url: string;
      readonly search?: string | null;
      readonly indexing?: string;
    }
  | {
      readonly event: "already_running";
      readonly protocol: number;
      readonly pid?: number | null;
      readonly url: string | null;
      readonly state: string | null;
    }
  | {
      readonly event: "child";
      readonly name: string;
      readonly state: string;
      readonly detail?: string | null;
      readonly exit_code?: number;
      readonly delay?: number;
    }
  | { readonly event: "address"; readonly url: string }
  | { readonly event: "stopping"; readonly reason: string; readonly detail?: string }
  | { readonly event: "stopped"; readonly exit_code: number }
  | { readonly event: "failed"; readonly detail: string };

export type ParsedLine =
  | { readonly kind: "event"; readonly event: LauncherEvent }
  | { readonly kind: "anomaly"; readonly reason: string };

type Fields = Record<string, unknown>;

const isString = (value: unknown): value is string => typeof value === "string";
const isNumber = (value: unknown): value is number => typeof value === "number";
const isStringOrNull = (value: unknown): value is string | null =>
  value === null || isString(value);

/** The fields each event must have, of the type it must have them. */
const REQUIRED: Record<LauncherEvent["event"], (fields: Fields) => boolean> = {
  starting: (f) => isNumber(f.protocol),
  phase: (f) => isString(f.phase),
  ready: (f) => isString(f.url),
  already_running: (f) =>
    isNumber(f.protocol) && isStringOrNull(f.url) && isStringOrNull(f.state),
  child: (f) => isString(f.name) && isString(f.state),
  address: (f) => isString(f.url),
  stopping: (f) => isString(f.reason),
  stopped: (f) => isNumber(f.exit_code),
  failed: (f) => isString(f.detail),
};

/** One line of the launcher's stdout. Anything that is not an event of protocol
 * 1 is an anomaly: written to the log, and otherwise ignored. */
export function parseLine(line: string): ParsedLine {
  let value: unknown;
  try {
    value = JSON.parse(line);
  } catch {
    return { kind: "anomaly", reason: "not JSON" };
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return { kind: "anomaly", reason: "not a JSON object" };
  }
  const fields = value as Fields;
  const name = fields.event;
  if (!isString(name) || !Object.hasOwn(REQUIRED, name)) {
    return {
      kind: "anomaly",
      reason: `an event this window does not know: ${String(name)}`,
    };
  }
  if (!REQUIRED[name as LauncherEvent["event"]](fields)) {
    return { kind: "anomaly", reason: `a ${name} event without the fields it needs` };
  }
  return { kind: "event", event: fields as unknown as LauncherEvent };
}

// ---------------------------------------------------------------------------
// What the start page shows

export type ErrorAction = "open-logs" | "reopen";

export type Screen =
  | { readonly kind: "progress"; readonly text: string }
  | {
      readonly kind: "error";
      readonly title: string;
      readonly detail: string;
      readonly lines: readonly string[];
      readonly actions: readonly ErrorAction[];
    };

export const OPENING = "Abrindo o SolidVision…";
export const RECONNECTING = "Reconectando…";
export const STOPPING = "Encerrando…";
export const WAITING = "Esperando outro SolidVision terminar de abrir ou de fechar…";

/** The text of the start screen after a phase, or `null` to keep the one shown.
 *
 * With the embedded database the `api` phase changes nothing: the API is
 * created together with the cluster (RFC-036 section 5.1), and the text would
 * go back from "Iniciando o servidor" to "Atualizando o banco". */
export function phaseText(
  phase: string | null,
  database: string | null,
): string | null {
  switch (phase) {
    case null:
      return OPENING;
    case "initdb":
      return "Preparando o banco pela primeira vez. Leva cerca de meio minuto.";
    case "starting":
      return "Iniciando o banco…";
    case "recovering":
      return "Recuperando o banco depois de um encerramento inesperado…";
    case "migrating":
      return "Atualizando o banco…";
    case "api":
      return database === "external" ? "Iniciando o servidor…" : null;
    default:
      return null;
  }
}

function progress(text: string): Screen {
  return { kind: "progress", text };
}

function error(
  title: string,
  detail: string,
  actions: readonly ErrorAction[],
  lines: readonly string[] = [],
): Screen {
  return { kind: "error", title, detail, lines, actions };
}

const NOT_OPENED = "O SolidVision não conseguiu abrir";
const STOPPED = "O SolidVision parou";

function exitedWith(code: number | null): string {
  return code === null ? "foi encerrado" : `saiu com o código ${code}`;
}

// ---------------------------------------------------------------------------
// The state machine

export type Stage =
  /** A launcher of this window was started, and has not said where the API is. */
  | "launching"
  /** Another launcher holds the data root, and is not ready yet. */
  | "waiting"
  /** The page is on its URL. */
  | "open"
  /** The page lost its origin, and the window waits for it to answer again. */
  | "reconnecting"
  /** The launcher is stopping. */
  | "stopping"
  /** Nothing more happens by itself: an error is on the screen. */
  | "ended";

export interface ShellState {
  readonly stage: Stage;
  readonly screen: Screen;
  /** A launcher process this window started is alive. */
  readonly running: boolean;
  /** That launcher holds the data root: closing the window stops it. */
  readonly owner: boolean;
  /** What that launcher said first. */
  readonly answer: "nothing" | "starting" | "already_running";
  readonly url: string | null;
  readonly database: string | null;
  readonly phase: string | null;
  readonly waitingSince: number | null;
  readonly reconnectingSince: number | null;
  /** The user closed the window, and the launcher was asked to stop. */
  readonly closing: boolean;
  /** The `detail` of a `stopping` the launcher began on its own, for a failure. */
  readonly failure: string | null;
}

export interface ShellOptions {
  /** `npm run dev`: after `ready`, the page comes from Vite. */
  readonly devServerUrl: string | null;
  /** ... whose proxy sends `/api` to this port. Another port is an error. */
  readonly devApiPort: number | null;
}

export const PRODUCTION: ShellOptions = { devServerUrl: null, devApiPort: null };

export type Input =
  /** A launcher process was created. */
  | { readonly kind: "launched" }
  | { readonly kind: "event"; readonly event: LauncherEvent }
  /** It exited; `lines` are the last of its stderr. */
  | {
      readonly kind: "exited";
      readonly code: number | null;
      readonly lines: readonly string[];
    }
  /** It could not be created: no Python, or the system refused. */
  | { readonly kind: "not-launched"; readonly detail: string }
  /** The page could not load its URL. */
  | { readonly kind: "load-failed" }
  /** Whether the page's URL answered a probe. */
  | { readonly kind: "probed"; readonly answered: boolean }
  /** The API answered the page's URL with a 404: it has no interface to serve,
   * which is `npm start` before `npm run build`. */
  | { readonly kind: "no-interface" }
  /** The user closes the window, past the dialog of section 6.4. */
  | { readonly kind: "close" }
  /** "Abrir de novo". */
  | { readonly kind: "reopen" };

export type Action =
  /** The start page, with this screen. */
  | { readonly kind: "show"; readonly screen: Screen }
  /** The page, at this URL; loaded again when it already is there. */
  | { readonly kind: "open"; readonly url: string }
  /** The event, to the page, through the bridge. */
  | { readonly kind: "forward"; readonly event: LauncherEvent }
  | { readonly kind: "launch"; readonly afterMs: number }
  | { readonly kind: "probe"; readonly url: string; readonly afterMs: number }
  /** Close the launcher's stdin; past the deadline, end it (section 6.4). */
  | { readonly kind: "stop" }
  | { readonly kind: "quit" };

export interface Step {
  readonly state: ShellState;
  readonly actions: readonly Action[];
}

export const INITIAL: ShellState = {
  stage: "launching",
  screen: progress(OPENING),
  running: false,
  owner: false,
  answer: "nothing",
  url: null,
  database: null,
  phase: null,
  waitingSince: null,
  reconnectingSince: null,
  closing: false,
  failure: null,
};

export function step(
  state: ShellState,
  input: Input,
  now: number,
  options: ShellOptions = PRODUCTION,
): Step {
  switch (input.kind) {
    case "launched":
      return { state: { ...state, running: true, answer: "nothing" }, actions: [] };
    case "event":
      return onEvent(state, input.event, now, options);
    case "exited":
      return onExit(state, input.code, input.lines);
    case "not-launched":
      return end(
        { ...state, running: false, owner: false },
        error(NOT_OPENED, input.detail, ["open-logs"]),
      );
    case "load-failed":
      return onLoadFailed(state, now, options);
    case "probed":
      return onProbe(state, input.answered, now, options);
    case "no-interface":
      if (state.stage !== "open") {
        return { state, actions: [] };
      }
      return end(
        state,
        error(
          "A interface não foi construída",
          `A API em ${state.url ?? "?"} não tem uma interface para servir: falta ` +
            "frontend\\dist\\index.html. Rode npm run build na pasta frontend e abra de novo.",
          ["open-logs"],
        ),
      );
    case "close":
      if (!state.running) {
        return { state: { ...state, closing: true }, actions: [{ kind: "quit" }] };
      }
      return show({ ...state, closing: true, stage: "stopping" }, progress(STOPPING), [
        { kind: "stop" },
      ]);
    case "reopen":
      if (state.stage !== "ended" || state.running || state.closing) {
        return { state, actions: [] };
      }
      return show({ ...INITIAL }, progress(OPENING), [{ kind: "launch", afterMs: 0 }]);
  }
}

function onEvent(
  state: ShellState,
  event: LauncherEvent,
  now: number,
  options: ShellOptions,
): Step {
  switch (event.event) {
    case "starting": {
      const answered: ShellState = { ...state, answer: "starting", owner: true };
      if (event.protocol !== PROTOCOL) {
        // It holds the data root, and its events cannot be read: it is stopped.
        return end(answered, protocolError(event.protocol), [{ kind: "stop" }]);
      }
      return { state: { ...answered, database: event.database ?? null }, actions: [] };
    }
    case "phase": {
      const next = { ...state, phase: event.phase };
      const text = phaseText(event.phase, state.database);
      if (text === null || state.stage !== "launching") {
        return { state: next, actions: [] };
      }
      return show(next, progress(text));
    }
    case "ready":
      return open({ ...state, owner: true, url: event.url }, options);
    case "already_running": {
      const answered: ShellState = {
        ...state,
        answer: "already_running",
        owner: false,
      };
      if (event.protocol !== PROTOCOL) {
        return end(answered, protocolError(event.protocol));
      }
      if (event.state === "ready" && event.url !== null) {
        return open({ ...answered, url: event.url, waitingSince: null }, options);
      }
      const since = state.waitingSince ?? now;
      if (now - since >= WAIT_FOR_ANOTHER_MS) {
        return end(
          answered,
          error(
            "Outro SolidVision não terminou de abrir",
            `Há ${WAIT_FOR_ANOTHER_MS / 1000} s outro SolidVision ocupa os dados deste ` +
              `usuário e não fica pronto (estado: ${event.state ?? "desconhecido"}).`,
            ["reopen", "open-logs"],
          ),
        );
      }
      return show(
        { ...answered, stage: "waiting", waitingSince: since },
        progress(WAITING),
        [{ kind: "launch", afterMs: RELAUNCH_DELAY_MS }],
      );
    }
    case "failed":
      return end(state, error(NOT_OPENED, event.detail, ["open-logs"]));
    case "child": {
      const forward: Action = { kind: "forward", event };
      if (event.name !== "api") {
        return { state, actions: [forward] };
      }
      if (event.state === "restarting" && state.stage === "open") {
        return show(
          { ...state, stage: "reconnecting", reconnectingSince: now },
          progress(RECONNECTING),
          [forward],
        );
      }
      if (
        event.state === "running" &&
        (state.stage === "open" || state.stage === "reconnecting")
      ) {
        const reopened = open({ ...state, reconnectingSince: null }, options);
        return { state: reopened.state, actions: [forward, ...reopened.actions] };
      }
      return { state, actions: [forward] };
    }
    case "address": {
      const moved: ShellState = { ...state, url: event.url };
      const forward: Action = { kind: "forward", event };
      if (state.stage !== "open" && state.stage !== "reconnecting") {
        return { state: moved, actions: [forward] };
      }
      const reopened = open(moved, options);
      return { state: reopened.state, actions: [forward, ...reopened.actions] };
    }
    case "stopping": {
      if (state.stage === "ended") {
        // An error is on the screen -- the window may have asked for this stop
        // itself, after a protocol it cannot read -- and it stays there.
        return { state, actions: [] };
      }
      const failure = event.reason === "failure" ? (event.detail ?? "") : null;
      return show({ ...state, stage: "stopping", failure }, progress(STOPPING));
    }
    case "stopped":
      if (state.closing) {
        return { state, actions: [{ kind: "quit" }] };
      }
      if (state.stage === "ended") {
        return { state, actions: [] };
      }
      if (state.failure === null) {
        // Asked by somebody else -- `python -m app.launcher stop`, or the end
        // of the session -- and the window goes with it.
        return { state, actions: [{ kind: "quit" }] };
      }
      return end(
        state,
        error(STOPPED, state.failure || "O launcher parou por uma falha.", [
          "reopen",
          "open-logs",
        ]),
      );
  }
}

function onExit(
  state: ShellState,
  code: number | null,
  lines: readonly string[],
): Step {
  const gone: ShellState = { ...state, running: false, owner: false };
  if (state.closing) {
    return { state: gone, actions: [{ kind: "quit" }] };
  }
  if (state.answer === "already_running" || state.stage === "ended") {
    // Expected: a second launcher says where the first is and leaves.
    return { state: gone, actions: [] };
  }
  if (state.stage === "launching") {
    return end(
      gone,
      error(
        NOT_OPENED,
        `O launcher ${exitedWith(code)} antes de ficar pronto. As últimas linhas que ele deixou:`,
        ["open-logs", "reopen"],
        lines,
      ),
    );
  }
  return end(
    gone,
    error(STOPPED, `O launcher ${exitedWith(code)}.`, ["reopen", "open-logs"], lines),
  );
}

function onLoadFailed(state: ShellState, now: number, options: ShellOptions): Step {
  const url = pageUrl(state, options);
  if (state.stage !== "open" || url === null) {
    return { state, actions: [] };
  }
  return show(
    { ...state, stage: "reconnecting", reconnectingSince: now },
    progress(RECONNECTING),
    [{ kind: "probe", url, afterMs: PROBE_DELAY_MS }],
  );
}

function onProbe(
  state: ShellState,
  answered: boolean,
  now: number,
  options: ShellOptions,
): Step {
  const url = pageUrl(state, options);
  if (state.stage !== "reconnecting" || url === null) {
    return { state, actions: [] };
  }
  if (answered) {
    return open({ ...state, reconnectingSince: null }, options);
  }
  const since = state.reconnectingSince ?? now;
  if (!state.running && now - since >= ADOPT_AFTER_MS) {
    return {
      state: { ...state, reconnectingSince: now },
      actions: [{ kind: "launch", afterMs: 0 }],
    };
  }
  return { state, actions: [{ kind: "probe", url, afterMs: PROBE_DELAY_MS }] };
}

// ---------------------------------------------------------------------------

/** The URL the page loads: the API's, or Vite's in `npm run dev`. */
export function pageUrl(state: ShellState, options: ShellOptions): string | null {
  if (state.url === null) {
    return null;
  }
  return options.devServerUrl ?? state.url;
}

function open(state: ShellState, options: ShellOptions): Step {
  const url = pageUrl(state, options);
  if (url === null || state.url === null) {
    return { state, actions: [] };
  }
  if (options.devApiPort !== null && portOf(state.url) !== options.devApiPort) {
    return end(state, devPortError(state.url, options.devApiPort));
  }
  return {
    state: { ...state, stage: "open", waitingSince: null, reconnectingSince: null },
    actions: [{ kind: "open", url }],
  };
}

function show(
  state: ShellState,
  screen: Screen,
  actions: readonly Action[] = [],
): Step {
  return {
    state: { ...state, screen },
    actions: [{ kind: "show", screen }, ...actions],
  };
}

function end(state: ShellState, screen: Screen, actions: readonly Action[] = []): Step {
  return show({ ...state, stage: "ended" }, screen, actions);
}

export function portOf(url: string): number | null {
  try {
    const parsed = new URL(url);
    if (parsed.port !== "") {
      return Number(parsed.port);
    }
    return parsed.protocol === "https:" ? 443 : 80;
  } catch {
    return null;
  }
}

function protocolError(got: number): Screen {
  return error(
    "Esta janela e o SolidVision não se entendem",
    `A janela fala o protocolo ${PROTOCOL} do launcher, e ele respondeu com o ` +
      `${got}: são de versões diferentes.`,
    ["open-logs"],
  );
}

function devPortError(url: string, port: number): Screen {
  return error(
    "A API não está onde o Vite procura",
    `A API está em ${url}, e o npm run dev encaminha /api para a porta ${port}. ` +
      `Pare o SolidVision que está na outra porta, ou inicie-o com API_PORT=${port}.`,
    ["open-logs"],
  );
}
