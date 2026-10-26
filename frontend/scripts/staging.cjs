// Where `npm run dist` stages the installer's `resources\` (RFC-041 section 7.2),
// read by `scripts/dist.mjs` and by `electron-builder.config.cjs` from this one
// place.
//
// `%LOCALAPPDATA%\SolidVision-build\resources`: outside the repository, which
// `stage_installation.py` requires, and outside `%TEMP%`, which RFC-040 section
// 14.1 saw being swept. A sibling of the data roots, never inside one: an
// uninstall leaves `SolidVision` whole, and a build has no business there.

const os = require("node:os");
const path = require("node:path");

const BUILD_DATA_NAME = "SolidVision-build";
const STAGED_MARKER = "SOLIDVISION-STAGED.json";

/** The montagem, by the backend's rule for `LOCALAPPDATA`. */
function stagingDirectory(environment = process.env) {
  const variable = environment.LOCALAPPDATA;
  const local =
    variable && path.isAbsolute(variable)
      ? variable
      : path.join(os.homedir(), "AppData", "Local");
  return path.join(local, BUILD_DATA_NAME, "resources");
}

module.exports = { BUILD_DATA_NAME, STAGED_MARKER, stagingDirectory };
