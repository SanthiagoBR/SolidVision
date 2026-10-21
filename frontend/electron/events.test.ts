import { describe, expect, it } from "vitest";

import {
  ADOPT_AFTER_MS,
  INITIAL,
  OPENING,
  RECONNECTING,
  RELAUNCH_DELAY_MS,
  STOPPING,
  WAIT_FOR_ANOTHER_MS,
  WAITING,
  parseLine,
  phaseText,
  step,
  type Action,
  type Input,
  type LauncherEvent,
  type Screen,
  type ShellOptions,
  type ShellState,
} from "./events";

const URL = "http://127.0.0.1:51234";
const VITE = "http://localhost:5173/";
const DEV: ShellOptions = { devServerUrl: VITE, devApiPort: 8000 };

/** Inputs in order, one millisecond apart unless a time is given. */
function run(
  inputs: readonly (Input | [Input, number])[],
  options?: ShellOptions,
  from: ShellState = INITIAL,
): { state: ShellState; actions: Action[]; last: readonly Action[] } {
  let state = from;
  const actions: Action[] = [];
  let last: readonly Action[] = [];
  inputs.forEach((entry, index) => {
    const [input, now] = Array.isArray(entry) ? entry : [entry, index];
    const result = step(state, input, now, options);
    state = result.state;
    last = result.actions;
    actions.push(...result.actions);
  });
  return { state, actions, last };
}

const event = (value: LauncherEvent): Input => ({ kind: "event", event: value });
const launched: Input = { kind: "launched" };
const starting = (database = "embedded"): Input =>
  event({ event: "starting", protocol: 1, database });
const ready = (url = URL): Input => event({ event: "ready", url, search: "loading" });
const exited = (code: number | null, lines: string[] = []): Input => ({
  kind: "exited",
  code,
  lines,
});

const open = (url: string): Action => ({ kind: "open", url });
const show = (screen: Screen): Action => ({ kind: "show", screen });
const progress = (text: string): Screen => ({ kind: "progress", text });

function errorOf(state: ShellState) {
  expect(state.stage).toBe("ended");
  expect(state.screen.kind).toBe("error");
  if (state.screen.kind !== "error") {
    throw new Error("not an error screen");
  }
  return state.screen;
}

describe("starting", () => {
  it("makes the window the owner, with protocol 1", () => {
    const { state, actions } = run([launched, starting()]);

    expect(state.owner).toBe(true);
    expect(state.database).toBe("embedded");
    expect(state.stage).toBe("launching");
    expect(actions).toEqual([]);
  });

  it("with an unknown protocol is an error, and the launcher is stopped", () => {
    const { state, actions } = run([
      launched,
      event({ event: "starting", protocol: 2 }),
    ]);

    const screen = errorOf(state);
    expect(screen.detail).toContain("protocolo 1");
    expect(screen.detail).toContain("com o 2");
    expect(screen.actions).toEqual(["open-logs"]);
    expect(actions).toContainEqual({ kind: "stop" });
  });

  it("keeps the error on the screen through the stop the window asked for", () => {
    const { state, actions } = run([
      launched,
      event({ event: "starting", protocol: 2 }),
      event({ event: "stopping", reason: "requested" }),
      event({ event: "stopped", exit_code: 0 }),
      exited(0),
    ]);

    expect(errorOf(state).title).toContain("não se entendem");
    expect(actions).not.toContainEqual({ kind: "quit" });
  });
});

describe("phase", () => {
  it.each([
    ["initdb", "Preparando o banco pela primeira vez. Leva cerca de meio minuto."],
    ["starting", "Iniciando o banco…"],
    ["recovering", "Recuperando o banco depois de um encerramento inesperado…"],
    ["migrating", "Atualizando o banco…"],
  ])("%s changes the text", (phase, text) => {
    const { state, last } = run([
      launched,
      starting(),
      event({ event: "phase", phase }),
    ]);

    expect(state.screen).toEqual(progress(text));
    expect(last).toEqual([show(progress(text))]);
  });

  it("api says the server is starting with the external database", () => {
    const { state } = run([
      launched,
      starting("external"),
      event({ event: "phase", phase: "api" }),
    ]);

    expect(state.screen).toEqual(progress("Iniciando o servidor…"));
  });

  it("api changes nothing with the embedded one, created with the cluster", () => {
    // The order of RFC-036 section 5.1, second branch: the API first.
    const { state } = run([
      launched,
      starting("embedded"),
      event({ event: "phase", phase: "api" }),
      event({ event: "phase", phase: "starting" }),
      event({ event: "phase", phase: "migrating" }),
      event({ event: "phase", phase: "job_runner" }),
    ]);

    expect(state.screen).toEqual(progress("Atualizando o banco…"));
  });

  it("before any phase, the window is opening", () => {
    expect(INITIAL.screen).toEqual(progress(OPENING));
    expect(phaseText(null, null)).toBe(OPENING);
    expect(phaseText("job_runner", "embedded")).toBeNull();
    expect(phaseText("a-phase-of-the-future", "embedded")).toBeNull();
  });
});

describe("ready", () => {
  it("opens the page at the url, and the window owns the launcher", () => {
    const { state, last } = run([launched, starting(), ready()]);

    expect(state.stage).toBe("open");
    expect(state.owner).toBe(true);
    expect(state.url).toBe(URL);
    expect(last).toEqual([open(URL)]);
  });

  it("opens Vite's page in npm run dev", () => {
    const { last } = run([launched, starting(), ready("http://127.0.0.1:8000")], DEV);

    expect(last).toEqual([open(VITE)]);
  });

  it("on another port than the proxy's, in npm run dev, is an error that says so", () => {
    const { state } = run([launched, starting(), ready("http://127.0.0.1:51234")], DEV);

    const screen = errorOf(state);
    expect(screen.detail).toContain("http://127.0.0.1:51234");
    expect(screen.detail).toContain("porta 8000");
  });
});

describe("already_running", () => {
  const another = (state: string | null, url: string | null = null): Input =>
    event({ event: "already_running", protocol: 1, pid: 4120, url, state });

  it("ready, with the url: the page, without owning the launcher", () => {
    const { state, last } = run([launched, another("ready", URL)]);

    expect(state.stage).toBe("open");
    expect(state.owner).toBe(false);
    expect(last).toEqual([open(URL)]);
  });

  it("whose own exit is expected, and whose close stops nothing", () => {
    const { state, actions } = run([
      launched,
      another("ready", URL),
      exited(0),
      { kind: "close" },
    ]);

    expect(state.running).toBe(false);
    expect(actions).not.toContainEqual({ kind: "stop" });
    expect(actions.at(-1)).toEqual({ kind: "quit" });
  });

  it("without a url, starting or stopping: the launcher again in 1 s", () => {
    const { state, last } = run([launched, another("starting")]);

    expect(state.stage).toBe("waiting");
    expect(state.screen).toEqual(progress(WAITING));
    expect(last).toContainEqual({ kind: "launch", afterMs: RELAUNCH_DELAY_MS });
  });

  it("not ready for 120 s is an error", () => {
    const tries: [Input, number][] = [];
    for (let now = 0; now <= WAIT_FOR_ANOTHER_MS; now += 1_000) {
      tries.push(
        [launched, now],
        [another("stopping"), now + 500],
        [exited(0), now + 600],
      );
    }
    const { state, actions } = run(tries);

    expect(errorOf(state).title).toContain("Outro SolidVision");
    const launches = actions.filter((action) => action.kind === "launch");
    expect(launches).toHaveLength(WAIT_FOR_ANOTHER_MS / 1_000);
  });

  it("that becomes ready on a later try opens the page", () => {
    const { state, last } = run([
      launched,
      another("starting"),
      exited(0),
      launched,
      another("ready", URL),
    ]);

    expect(state.stage).toBe("open");
    expect(last).toEqual([open(URL)]);
  });

  it("with an unknown protocol is an error", () => {
    const { state } = run([
      launched,
      event({
        event: "already_running",
        protocol: 7,
        pid: 1,
        url: URL,
        state: "ready",
      }),
    ]);

    expect(errorOf(state).detail).toContain("com o 7");
  });
});

describe("failed", () => {
  it("is an error with the line and the logs folder, and the exit 78 after it changes nothing", () => {
    const line = "SolidVision cannot start: .env: DATABASE_PORT: not a number";
    const { state } = run([
      launched,
      starting(),
      event({ event: "failed", detail: line }),
      exited(78),
    ]);

    const screen = errorOf(state);
    expect(screen.detail).toBe(line);
    expect(screen.actions).toEqual(["open-logs"]);
  });
});

describe("child", () => {
  const child = (name: string, state: string): LauncherEvent => ({
    event: "child",
    name,
    state,
    detail: "the last line",
  });

  it("is passed on to the page", () => {
    const unavailable = child("job_runner", "unavailable");
    const { state, last } = run([launched, starting(), ready(), event(unavailable)]);

    expect(state.stage).toBe("open");
    expect(last).toEqual([{ kind: "forward", event: unavailable }]);
  });

  it("api restarting is Reconectando…, and api running reloads the page", () => {
    const restarting = child("api", "restarting");
    const running = child("api", "running");
    const during = run([launched, starting(), ready(), event(restarting)]);

    expect(during.state.stage).toBe("reconnecting");
    expect(during.last).toEqual([
      show(progress(RECONNECTING)),
      { kind: "forward", event: restarting },
    ]);

    const after = run([event(running)], undefined, during.state);
    expect(after.state.stage).toBe("open");
    expect(after.last).toEqual([{ kind: "forward", event: running }, open(URL)]);
  });
});

describe("the job runner, kept for the page (RFC-039 section 5.9)", () => {
  const readyWith = (indexing: string): Input =>
    event({ event: "ready", url: URL, search: "loading", indexing });
  const runner = (state: string, detail: string | null = null): Input =>
    event({ event: "child", name: "job_runner", state, detail });

  it("is nothing before the launcher said anything", () => {
    expect(INITIAL.indexing).toBeNull();
    expect(run([launched, starting()]).state.indexing).toBeNull();
  });

  it("is the indexing of ready, without a line", () => {
    const { state } = run([launched, starting(), readyWith("running")]);

    expect(state.indexing).toEqual({ state: "running", detail: null });
  });

  it("follows each child named job_runner, with its line", () => {
    const { state } = run([
      launched,
      starting(),
      readyWith("running"),
      runner("restarting", "Traceback: the last line"),
      runner("running"),
      runner("unavailable", "SolidVision cannot start: the models are not in X"),
    ]);

    expect(state.indexing).toEqual({
      state: "unavailable",
      detail: "SolidVision cannot start: the models are not in X",
    });
  });

  it("keeps the line of a runner that refused to start before ready", () => {
    // Exit 78, the models missing: the child comes first, and ready repeats
    // the state without the line (RFC-035 section 6.5).
    const line = "SolidVision cannot start: The models are not in C:\\vazio";
    const { state } = run([
      launched,
      starting(),
      runner("unavailable", line),
      readyWith("unavailable"),
    ]);

    expect(state.indexing).toEqual({ state: "unavailable", detail: line });
  });

  it("drops an old line when ready says something else", () => {
    const { state } = run([
      launched,
      starting(),
      runner("restarting", "an old line"),
      readyWith("running"),
    ]);

    expect(state.indexing).toEqual({ state: "running", detail: null });
  });

  it("ignores the other children", () => {
    const { state } = run([
      launched,
      starting(),
      readyWith("running"),
      event({ event: "child", name: "api", state: "restarting", detail: "x" }),
      event({ event: "child", name: "database", state: "unavailable", detail: "y" }),
    ]);

    expect(state.indexing).toEqual({ state: "running", detail: null });
  });

  it("is cleared by a new launcher", () => {
    const before = run([launched, starting(), runner("unavailable", "the line")]);

    expect(run([launched], undefined, before.state).state.indexing).toBeNull();
  });

  it("is nothing for a window that does not own the launcher", () => {
    const { state } = run([
      launched,
      event({
        event: "already_running",
        protocol: 1,
        pid: 4120,
        url: URL,
        state: "ready",
      }),
    ]);

    expect(state.owner).toBe(false);
    expect(state.indexing).toBeNull();
  });

  it("is still passed on to an open page, as every child is", () => {
    const unavailable: LauncherEvent = {
      event: "child",
      name: "job_runner",
      state: "unavailable",
      detail: "the line",
    };
    const { last } = run([launched, starting(), ready(), event(unavailable)]);

    expect(last).toEqual([{ kind: "forward", event: unavailable }]);
  });
});

describe("address", () => {
  it("opens the page at the new url", () => {
    const moved: LauncherEvent = { event: "address", url: "http://127.0.0.1:52000" };
    const { state, last } = run([launched, starting(), ready(), event(moved)]);

    expect(state.url).toBe("http://127.0.0.1:52000");
    expect(last).toEqual([
      { kind: "forward", event: moved },
      open("http://127.0.0.1:52000"),
    ]);
  });
});

describe("stopping and stopped", () => {
  it("asked by somebody else: Encerrando…, and the window quits", () => {
    const { actions } = run([
      launched,
      starting(),
      ready(),
      event({ event: "stopping", reason: "requested" }),
      event({ event: "stopped", exit_code: 0 }),
    ]);

    expect(actions).toContainEqual(show(progress(STOPPING)));
    expect(actions.at(-1)).toEqual({ kind: "quit" });
  });

  it("for a failure: the window stays, says why, and offers to open again", () => {
    const detail = "api exited 5 times in 10 minutes, and is not restarted again.";
    const { state, actions } = run([
      launched,
      starting(),
      ready(),
      event({ event: "stopping", reason: "failure", detail }),
      event({ event: "stopped", exit_code: 78 }),
      exited(78),
    ]);

    const screen = errorOf(state);
    expect(screen.title).toBe("O SolidVision parou");
    expect(screen.detail).toBe(detail);
    expect(screen.actions).toEqual(["reopen", "open-logs"]);
    expect(actions).not.toContainEqual({ kind: "quit" });
  });
});

describe("an exit without failed or stopped", () => {
  const lines = [
    "Traceback (most recent call last):",
    "ImportError: no module named app",
  ];

  it("before ready is an error with the last lines of stderr", () => {
    const { state } = run([launched, exited(1, lines)]);

    const screen = errorOf(state);
    expect(screen.title).toBe("O SolidVision não conseguiu abrir");
    expect(screen.detail).toContain("saiu com o código 1 antes de ficar pronto");
    expect(screen.lines).toEqual(lines);
    expect(screen.actions).toContain("open-logs");
  });

  it("after ready is O SolidVision parou, with Abrir de novo", () => {
    const { state } = run([launched, starting(), ready(), exited(null, lines)]);

    const screen = errorOf(state);
    expect(screen.title).toBe("O SolidVision parou");
    expect(screen.detail).toContain("foi encerrado");
    expect(screen.actions[0]).toBe("reopen");
    expect(state.owner).toBe(false);
  });

  it("is opened again by Abrir de novo", () => {
    const { state, last } = run([
      launched,
      starting(),
      ready(),
      exited(1),
      { kind: "reopen" },
    ]);

    expect(state.stage).toBe("launching");
    expect(state.screen).toEqual(progress(OPENING));
    expect(last).toEqual([show(progress(OPENING)), { kind: "launch", afterMs: 0 }]);
  });

  it("a launcher that could not be created is an error with its line", () => {
    const line = "Nenhum Python do SolidVision foi encontrado: nem a, nem b.";
    const { state } = run([{ kind: "not-launched", detail: line }]);

    expect(errorOf(state).detail).toBe(line);
  });
});

describe("closing (section 6.4)", () => {
  it("asks the launcher to stop, says Encerrando…, and quits on stopped", () => {
    const { actions } = run([
      launched,
      starting(),
      ready(),
      { kind: "close" },
      event({ event: "stopping", reason: "requested" }),
      event({ event: "stopped", exit_code: 0 }),
    ]);

    expect(actions).toContainEqual({ kind: "stop" });
    expect(actions).toContainEqual(show(progress(STOPPING)));
    expect(actions.at(-1)).toEqual({ kind: "quit" });
  });

  it("quits on the exit, when no stopped came: the 60 s deadline killed it", () => {
    const { last } = run([launched, starting(), ready(), { kind: "close" }, exited(1)]);

    expect(last).toEqual([{ kind: "quit" }]);
  });

  it("while it is still starting stops it too", () => {
    const { actions } = run([launched, { kind: "close" }]);

    expect(actions).toContainEqual({ kind: "stop" });
  });

  it("with nothing running quits at once", () => {
    const { last } = run([{ kind: "close" }]);

    expect(last).toEqual([{ kind: "quit" }]);
  });
});

describe("a page that does not load (section 11's risk)", () => {
  it("is Reconectando…, probed every second, and opened when it answers", () => {
    const opened = run([launched, starting(), ready()]);
    const failing = run([{ kind: "load-failed" }], undefined, opened.state);

    expect(failing.state.stage).toBe("reconnecting");
    expect(failing.last).toEqual([
      show(progress(RECONNECTING)),
      { kind: "probe", url: URL, afterMs: 1_000 },
    ]);
    const still = run([{ kind: "probed", answered: false }], undefined, failing.state);
    expect(still.last).toEqual([{ kind: "probe", url: URL, afterMs: 1_000 }]);
    const back = run([{ kind: "probed", answered: true }], undefined, still.state);
    expect(back.state.stage).toBe("open");
    expect(back.last).toEqual([open(URL)]);
  });

  it("without owning the launcher, for 30 s, starts one", () => {
    const another: Input = event({
      event: "already_running",
      protocol: 1,
      pid: 1,
      url: URL,
      state: "ready",
    });
    const { state, last } = run([
      [launched, 0],
      [another, 1],
      [exited(0), 2],
      [{ kind: "load-failed" }, 10],
      [{ kind: "probed", answered: false }, 1_000],
      [{ kind: "probed", answered: false }, 10 + ADOPT_AFTER_MS],
    ]);

    expect(state.stage).toBe("reconnecting");
    expect(last).toEqual([{ kind: "launch", afterMs: 0 }]);
  });

  it("answering 404, which is npm start before npm run build, is an error", () => {
    const { state } = run([launched, starting(), ready(), { kind: "no-interface" }]);

    expect(errorOf(state).detail).toContain("npm run build");
  });
});

describe("parseLine", () => {
  it("reads every event of the protocol", () => {
    const lines = [
      '{"event": "starting", "protocol": 1, "pid": 4120, "mode": "development", "database": "embedded"}',
      '{"event": "phase", "phase": "initdb"}',
      '{"event": "ready", "url": "http://127.0.0.1:51234", "search": "loading", "indexing": "running"}',
      '{"event": "already_running", "protocol": 1, "pid": null, "url": null, "state": null}',
      '{"event": "child", "name": "job_runner", "state": "unavailable", "exit_code": 78, "detail": "x"}',
      '{"event": "address", "url": "http://127.0.0.1:52000"}',
      '{"event": "stopping", "reason": "requested"}',
      '{"event": "stopped", "exit_code": 0}',
      '{"event": "failed", "detail": "SolidVision cannot start: ..."}',
    ];

    for (const line of lines) {
      expect(parseLine(line).kind, line).toBe("event");
    }
  });

  it.each([
    ["Uvicorn running on http://127.0.0.1:8000", "not JSON"],
    ["[1, 2]", "not a JSON object"],
    ['{"event": "teleported"}', "an event this window does not know: teleported"],
    ['{"event": "ready"}', "a ready event without the fields it needs"],
    [
      '{"event": "starting", "protocol": "1"}',
      "a starting event without the fields it needs",
    ],
  ])("calls %s an anomaly", (line, reason) => {
    expect(parseLine(line)).toEqual({ kind: "anomaly", reason });
  });

  it("tolerates the carriage return of a Windows pipe", () => {
    expect(parseLine('{"event": "stopped", "exit_code": 0}\r').kind).toBe("event");
  });
});
