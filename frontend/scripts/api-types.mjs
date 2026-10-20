// `npm run api:types`: the OpenAPI document from the API, then the types from it
// (RFC-037 section 4.2). The only command of the frontend that needs Python,
// which it finds the way the window does: `resolvePython()`, compiled by the
// `build:electron` this script runs after.

import { execFileSync } from "node:child_process";
import { existsSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { generateSchemaTypes, SCHEMA_TYPES } from "./schema-types.mjs";

const require = createRequire(import.meta.url);
const { resolvePython } = require("../dist-electron/python.js");

const frontend = dirname(dirname(fileURLToPath(import.meta.url)));
const repositoryRoot = dirname(frontend);

const resolution = resolvePython({
  packaged: false,
  repositoryRoot,
  environment: process.env,
  exists: existsSync,
});
if (!resolution.ok) {
  console.error(resolution.line);
  process.exit(1);
}
execFileSync(
  resolution.command.executable,
  [join(repositoryRoot, "scripts", "export_openapi.py")],
  { cwd: repositoryRoot, env: resolution.command.env, stdio: "inherit" },
);
writeFileSync(SCHEMA_TYPES, await generateSchemaTypes());
console.log(`${fileURLToPath(SCHEMA_TYPES)}: written`);
