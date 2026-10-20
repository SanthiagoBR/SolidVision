// The Electron binary, run as the window and never as plain Node.
//
// A process started from VS Code's extension host inherits
// ELECTRON_RUN_AS_NODE=1, and `electron .` would then run main.js as a Node
// script, with no `app` and no window. `npm start` and `npm run dev` go
// through here, without it.

import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const frontend = dirname(dirname(fileURLToPath(import.meta.url)));

/** @param {Record<string, string>} [extra] variables added to the environment */
export function runElectron(extra = {}) {
  const environment = { ...process.env, ...extra };
  delete environment.ELECTRON_RUN_AS_NODE;
  return spawn(require("electron"), ["."], {
    cwd: frontend,
    env: environment,
    stdio: "inherit",
  });
}
