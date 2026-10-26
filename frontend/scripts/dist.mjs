// `npm run dist` and `npm run dist:dir` (RFC-041 section 7.2).
//
//     1. npm run build, as always.
//     2. The staged installation, as the `resources` folder: the backend, the
//        interface and the three runtimes, outside the repository, compiled by
//        the package's own Python (`scripts/stage_installation.py`).
//     3. The checks of section 7.3 on it (`scripts/check_package.py`), the
//        vectors by the package's Python included. Any failure stops here.
//     4. electron-builder --dir: the folder, `release/win-unpacked`, and the
//        checks of the folder -- the path with the Electron files, the DLLs of
//        the three runtimes, `resources` the montagem's and the `app.asar`.
//     5. Unless --dir: electron-builder makes the NSIS installer of that very
//        folder (`--prepackaged`), `release/SolidVision-Setup-<version>.exe`.
//
// `--dir` stops after 4: the package to look at without waiting for the
// compression, and the second cut of the roadmap (section 13).
//
// Python is the development virtualenv's, found the way the window finds it
// (`resolvePython()`), and it runs the two scripts; the package's own runs the
// vectors. Every step's time and the sizes go to `release/dist.log`, beside
// what each step printed: the build's time is the roadmap's second risk.

import { spawn } from "node:child_process";
import {
  createWriteStream,
  existsSync,
  mkdirSync,
  readFileSync,
  statSync,
} from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { performance } from "node:perf_hooks";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { STAGED_MARKER, stagingDirectory } = require("./staging.cjs");

const frontend = dirname(dirname(fileURLToPath(import.meta.url)));
const repositoryRoot = dirname(frontend);
const release = join(frontend, "release");
const unpacked = join(release, "win-unpacked");
const dirOnly = process.argv.includes("--dir");
const { version } = JSON.parse(readFileSync(join(frontend, "package.json"), "utf8"));
const installer = join(release, `SolidVision-Setup-${version}.exe`);

mkdirSync(release, { recursive: true });
const log = createWriteStream(join(release, "dist.log"), { flags: "w" });

function say(line) {
  console.log(line);
  log.write(`${line}\n`);
}

/** One child, its output to the console and to the log; rejects on failure. */
function run(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: options.cwd ?? frontend,
      env: options.env ?? process.env,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    });
    for (const stream of [child.stdout, child.stderr]) {
      stream.on("data", (chunk) => {
        process.stdout.write(chunk);
        log.write(chunk);
      });
    }
    child.on("error", reject);
    child.on("exit", (code) =>
      code === 0
        ? resolve()
        : reject(new Error(`${command} ${args.join(" ")} exited with ${code}`)),
    );
  });
}

async function step(name, action) {
  say(`\n== ${name}`);
  const started = performance.now();
  await action();
  const seconds = (performance.now() - started) / 1000;
  say(`== ${name}: ${seconds.toFixed(1)} s`);
  return seconds;
}

function npmCli() {
  const cli = process.env.npm_execpath;
  if (!cli || !cli.endsWith(".js") || !existsSync(cli)) {
    throw new Error("Run this through npm: `npm run dist` or `npm run dist:dir`.");
  }
  return cli;
}

function developmentPython() {
  const { resolvePython } = require("../dist-electron/python.js");
  const resolution = resolvePython({
    packaged: false,
    repositoryRoot,
    environment: process.env,
    exists: existsSync,
  });
  if (!resolution.ok) {
    throw new Error(resolution.line);
  }
  return resolution.command;
}

function electronBuilder(args) {
  return run(process.execPath, [
    join(frontend, "node_modules", "electron-builder", "cli.js"),
    "--config",
    "electron-builder.config.cjs",
    "--win",
    "--x64",
    ...args,
  ]);
}

function gigabytes(bytes) {
  return `${(bytes / 2 ** 30).toFixed(2)} GB (${bytes} bytes)`;
}

async function main() {
  const started = performance.now();
  const staging = stagingDirectory();
  const times = {};
  say(`SolidVision ${version}: ${dirOnly ? "the folder" : "the installer"}`);
  say(`the montagem: ${staging}`);

  times.build = await step("1. npm run build", () =>
    run(process.execPath, [npmCli(), "run", "build"]),
  );
  // Compiled by the step above: the same function the window uses.
  const python = developmentPython();
  const scripts = join(repositoryRoot, "scripts");
  const pythonRun = (script, args) =>
    run(python.executable, ["-X", "utf8", join(scripts, script), ...args], {
      cwd: repositoryRoot,
      env: python.env,
    });

  times.stage = await step("2. the montagem", async () => {
    if (existsSync(join(staging, STAGED_MARKER))) {
      await pythonRun("stage_installation.py", [staging, "--remove"]);
    }
    await pythonRun("stage_installation.py", [staging]);
  });
  times.checks = await step("3. the checks of the montagem", () =>
    pythonRun("check_package.py", ["--staging", staging]),
  );
  times.folder = await step(
    "4. electron-builder --dir, and the checks of the folder",
    async () => {
      await electronBuilder(["--dir"]);
      await pythonRun("check_package.py", [
        "--staging",
        staging,
        "--unpacked",
        unpacked,
      ]);
    },
  );
  if (!dirOnly) {
    times.installer = await step("5. electron-builder: the NSIS installer", () =>
      electronBuilder(["--prepackaged", unpacked]),
    );
    say(`the installer: ${installer}, ${gigabytes(statSync(installer).size)}`);
  }
  const total = (performance.now() - started) / 1000;
  say(
    `\ndist: ${total.toFixed(0)} s in all (` +
      Object.entries(times)
        .map(([name, seconds]) => `${name} ${seconds.toFixed(0)} s`)
        .join(", ") +
      ")",
  );
}

try {
  await main();
} catch (error) {
  say(`dist: FAILED: ${error instanceof Error ? error.message : String(error)}`);
  process.exitCode = 1;
} finally {
  log.end();
}
