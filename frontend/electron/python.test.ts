import { describe, expect, it } from "vitest";

import {
  developmentCandidates,
  LAUNCHER_ARGUMENTS,
  resolvePython,
  type Where,
} from "./python";

const ROOT = "C:\\Users\\ana\\SolidVision";
const DOT_VENV = "C:\\Users\\ana\\SolidVision\\.venv\\Scripts\\python.exe";
const BACKEND_VENV = "C:\\Users\\ana\\SolidVision\\backend\\.venv\\Scripts\\python.exe";

function where(
  present: readonly string[],
  environment: Record<string, string> = {},
): Where {
  return {
    packaged: false,
    repositoryRoot: ROOT,
    environment,
    exists: (file) => present.includes(file),
    platform: "win32",
  };
}

describe("resolvePython() in development", () => {
  it("tries the README's .venv first, then backend\\.venv", () => {
    expect(developmentCandidates(ROOT, "win32")).toEqual([DOT_VENV, BACKEND_VENV]);
  });

  it("takes .venv when both exist", () => {
    const resolution = resolvePython(where([DOT_VENV, BACKEND_VENV]));

    expect(resolution.ok && resolution.command.executable).toBe(DOT_VENV);
  });

  it("takes backend\\.venv when it is the only one, as on the machine of the RFC", () => {
    const resolution = resolvePython(where([BACKEND_VENV]));

    expect(resolution.ok && resolution.command.executable).toBe(BACKEND_VENV);
  });

  it("takes SOLIDVISION_PYTHON before either", () => {
    const chosen = "D:\\python\\python.exe";
    const resolution = resolvePython(
      where([chosen, DOT_VENV], { SOLIDVISION_PYTHON: chosen }),
    );

    expect(resolution.ok && resolution.command.executable).toBe(chosen);
  });

  it("never replaces a SOLIDVISION_PYTHON that does not exist with another one", () => {
    const resolution = resolvePython(
      where([DOT_VENV], { SOLIDVISION_PYTHON: "D:\\nowhere\\python.exe" }),
    );

    expect(resolution).toEqual({
      ok: false,
      line: "SOLIDVISION_PYTHON é D:\\nowhere\\python.exe, e esse arquivo não existe.",
    });
  });

  it("says which it tried, and what to do, when none exists", () => {
    const resolution = resolvePython(where([]));

    expect(resolution.ok).toBe(false);
    if (!resolution.ok) {
      expect(resolution.line).toContain(DOT_VENV);
      expect(resolution.line).toContain(BACKEND_VENV);
      expect(resolution.line).toContain("SOLIDVISION_PYTHON");
    }
  });

  it("runs the launcher supervised from the root, with backend on the path", () => {
    const resolution = resolvePython(where([BACKEND_VENV], { PATH: "C:\\Windows" }));

    expect(resolution.ok).toBe(true);
    if (resolution.ok) {
      expect(resolution.command.args).toEqual(["-m", "app.launcher", "--supervised"]);
      expect(resolution.command.args).toBe(LAUNCHER_ARGUMENTS);
      expect(resolution.command.cwd).toBe(ROOT);
      expect(resolution.command.env.PYTHONPATH).toBe(`${ROOT}\\backend`);
      expect(resolution.command.env.PATH).toBe("C:\\Windows");
    }
  });

  it("puts backend before a PYTHONPATH the environment already had", () => {
    const resolution = resolvePython(where([BACKEND_VENV], { PYTHONPATH: "D:\\libs" }));

    expect(resolution.ok && resolution.command.env.PYTHONPATH).toBe(
      `${ROOT}\\backend;D:\\libs`,
    );
  });
});

describe("resolvePython() installed", () => {
  it("is RFC-041's, and fails with a line that says so", () => {
    const resolution = resolvePython({ ...where([DOT_VENV]), packaged: true });

    expect(resolution.ok).toBe(false);
    if (!resolution.ok) {
      expect(resolution.line).toContain("RFC-041");
    }
  });
});
