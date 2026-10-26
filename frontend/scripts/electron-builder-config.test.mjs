// The installer's configuration (RFC-041 sections 7.1 and 8): what makes it a
// per-user installer that never asks for elevation, and what goes into it.

import { createRequire } from "node:module";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const require = createRequire(import.meta.url);
const config = require("../electron-builder.config.cjs");
const { STAGED_MARKER, stagingDirectory } = require("./staging.cjs");
const { version } = require("../package.json");

describe("the NSIS installer (section 7.1)", () => {
  it("is one click, per user, and never elevates", () => {
    expect(config.nsis.oneClick).toBe(true);
    expect(config.nsis.perMachine).toBe(false);
    expect(config.nsis.allowElevation).toBe(false);
    expect(config.nsis.packElevateHelper).toBe(false);
  });

  it("opens the app when it ends, and leaves the data on uninstall", () => {
    expect(config.nsis.runAfterFinish).toBe(true);
    expect(config.nsis.deleteAppDataOnUninstall).toBe(false);
  });

  it("asks the launcher to stop before touching a file (section 7.4)", () => {
    expect(config.nsis.include).toBe("build/installer.nsh");
  });

  it("is named after the version, with no blockmap and nothing published", () => {
    expect(config.nsis.artifactName).toBe("SolidVision-Setup-${version}.${ext}");
    expect(config.nsis.differentialPackage).toBe(false);
    expect(config.publish).toBeNull();
    expect(version).toMatch(/^\d+\.\d+\.\d+$/);
  });

  it("installs into Programs\\SolidVision, not npm's lower-case name", () => {
    expect(config.productName).toBe("SolidVision");
    expect(config.extraMetadata.name).toBe("SolidVision");
  });

  it("is an x64 NSIS target", () => {
    expect(config.win.target).toEqual([{ target: "nsis", arch: ["x64"] }]);
  });
});

describe("what goes into the package (section 4)", () => {
  it("puts only the shell in app.asar, and no node_modules", () => {
    expect(config.asar).toBe(true);
    expect(config.files).toEqual([
      "dist-electron/**/*",
      "electron/startup.html",
      "package.json",
      "!node_modules/**/*",
    ]);
  });

  it("copies the montagem as resources, without its marker", () => {
    expect(config.extraResources).toEqual([
      { from: stagingDirectory(), to: ".", filter: ["**/*", `!${STAGED_MARKER}`] },
    ]);
  });

  it("turns off the three fuses of section 6.3", () => {
    expect(config.electronFuses).toEqual({
      runAsNode: false,
      enableNodeOptionsEnvironmentVariable: false,
      enableNodeCliInspectArguments: false,
    });
  });
});

describe("the montagem's place (section 7.2)", () => {
  it("is SolidVision-build\\resources under LOCALAPPDATA, outside the repository", () => {
    const local = "C:\\Users\\ana\\AppData\\Local";

    expect(stagingDirectory({ LOCALAPPDATA: local })).toBe(
      join(local, "SolidVision-build", "resources"),
    );
  });

  it("is never inside a data root, which an uninstall leaves whole", () => {
    const local = "C:\\Users\\ana\\AppData\\Local";
    const staging = stagingDirectory({ LOCALAPPDATA: local });

    for (const root of ["SolidVision", "SolidVision-dev"]) {
      expect(staging.startsWith(join(local, root) + "\\")).toBe(false);
    }
  });
});
