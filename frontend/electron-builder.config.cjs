// The installer (RFC-041 section 7.1): electron-builder 26.18.0, NSIS, one click,
// per user, never elevated, in %LOCALAPPDATA%\Programs\SolidVision.
//
// The app is two things. The shell -- the main process and the start page --
// goes into `app.asar`, with no `node_modules`: the page's dependencies are in
// the interface's build, and the main process imports Electron and Node only.
// Everything else is the montagem of `scripts/dist.mjs`, copied as
// `resources\`, the root of the layout (section 4), without its marker.

const { STAGED_MARKER, stagingDirectory } = require("./scripts/staging.cjs");

/** @type {import("electron-builder").Configuration} */
module.exports = {
  appId: "io.github.santhiagobr.solidvision",
  productName: "SolidVision",
  executableName: "SolidVision",
  // The installation folder is named after `name` in a one-click per-user
  // installer, and npm's is lower case: this keeps it `Programs\SolidVision`.
  // Nothing at run time reads it; `app.setName()` names the app.
  extraMetadata: { name: "SolidVision" },
  directories: { output: "release", buildResources: "build" },
  files: [
    "dist-electron/**/*",
    "electron/startup.html",
    "package.json",
    "!node_modules/**/*",
  ],
  asar: true,
  extraResources: [
    {
      from: stagingDirectory(),
      to: ".",
      filter: ["**/*", `!${STAGED_MARKER}`],
    },
  ],
  // What Chromium falls back to when the system's language is missing. The
  // interface is PT-BR only.
  electronLanguages: ["pt-BR", "en-US"],
  // No native module to rebuild, and no dependency of the shell to install.
  npmRebuild: false,
  nodeGypRebuild: false,
  // Section 6.3: the environment of the person does not change what the
  // installed app is. ELECTRON_RUN_AS_NODE, NODE_OPTIONS and --inspect.
  electronFuses: {
    runAsNode: false,
    enableNodeOptionsEnvironmentVariable: false,
    enableNodeCliInspectArguments: false,
  },
  win: {
    target: [{ target: "nsis", arch: ["x64"] }],
    icon: "build/icon.png",
  },
  nsis: {
    oneClick: true,
    perMachine: false,
    // Not the default: it is what guarantees the installer never asks for
    // elevation (RFC-034 section 13, RFC-036 section 14).
    allowElevation: false,
    packElevateHelper: false,
    runAfterFinish: true,
    createDesktopShortcut: true,
    createStartMenuShortcut: true,
    shortcutName: "SolidVision",
    // The data stay (section 7.4); this would delete Electron's userData, and
    // not the data root, anyway.
    deleteAppDataOnUninstall: false,
    include: "build/installer.nsh",
    artifactName: "SolidVision-Setup-${version}.${ext}",
    // No automatic update, so no differential package and no blockmap.
    differentialPackage: false,
  },
  publish: null,
  compression: "normal",
};
