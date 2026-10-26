/**
 * Where the Python is, and how the launcher is run with it (RFC-037 section 6.2).
 *
 *     <python> -m app.launcher --supervised
 *
 * `resolvePython()` is the one function that knows where the interpreter is,
 * as `child_command()` is the one that builds the children's command line
 * (RFC-036 section 5.5): installing changes this function and nothing else.
 *
 *     mode          the Python                         the directory, the env
 *     development   SOLIDVISION_PYTHON when set; else  the repository root, with
 *                   .venv (the README); else           PYTHONPATH=backend
 *                   backend\.venv (this machine)       (RFC-036 section 9)
 *     installed     resources\runtime\python\          resources\, and the
 *                   python.exe, and nothing else       window's own environment
 *                   (RFC-041 section 6.1)
 *
 * Installed, no variable chooses the interpreter: the environment is not a
 * source of an installed app (RFC-034 section 5.2). And nothing is added to
 * the environment, because the embedded Python reads its paths from its
 * `python312._pth` and ignores `PYTHONPATH` (RFC-041 section 2.8).
 *
 * Nothing here touches the disk but `exists`, which the tests replace.
 */

import * as path from "node:path";

export const LAUNCHER_ARGUMENTS: readonly string[] = [
  "-m",
  "app.launcher",
  "--supervised",
];

export interface LauncherCommand {
  readonly executable: string;
  readonly args: readonly string[];
  readonly cwd: string;
  readonly env: Readonly<Record<string, string | undefined>>;
}

export type Resolution =
  | { readonly ok: true; readonly command: LauncherCommand }
  | { readonly ok: false; readonly line: string };

export interface Where {
  readonly packaged: boolean;
  /** The repository root, in development: the parent of `frontend`. */
  readonly repositoryRoot: string;
  /** Electron's `process.resourcesPath`, installed: the root of the layout. */
  readonly resourcesPath?: string;
  readonly environment: Readonly<Record<string, string | undefined>>;
  readonly exists: (file: string) => boolean;
  readonly platform?: NodeJS.Platform;
}

/** The interpreter of a virtual environment, by the platform's layout. */
function venvPython(venv: string, platform: NodeJS.Platform): string {
  const join = platform === "win32" ? path.win32.join : path.posix.join;
  return platform === "win32"
    ? join(venv, "Scripts", "python.exe")
    : join(venv, "bin", "python");
}

/** The candidates of development, in the order they are tried. */
export function developmentCandidates(
  repositoryRoot: string,
  platform: NodeJS.Platform = process.platform,
): readonly string[] {
  const join = platform === "win32" ? path.win32.join : path.posix.join;
  return [
    venvPython(join(repositoryRoot, ".venv"), platform),
    venvPython(join(repositoryRoot, "backend", ".venv"), platform),
  ];
}

/** The installed interpreter, under the root of the layout. */
export function installedPython(
  resourcesPath: string,
  platform: NodeJS.Platform = process.platform,
): string {
  const join = platform === "win32" ? path.win32.join : path.posix.join;
  return join(resourcesPath, "runtime", "python", "python.exe");
}

export function resolvePython(where: Where): Resolution {
  const platform = where.platform ?? process.platform;
  if (where.packaged) {
    return installedResolution(where, platform);
  }
  const executable = developmentPython(where, platform);
  if (!executable.ok) {
    return executable;
  }
  const join = platform === "win32" ? path.win32.join : path.posix.join;
  const delimiter = platform === "win32" ? path.win32.delimiter : path.posix.delimiter;
  const backend = join(where.repositoryRoot, "backend");
  const inherited = where.environment.PYTHONPATH;
  return {
    ok: true,
    command: {
      executable: executable.path,
      args: LAUNCHER_ARGUMENTS,
      cwd: where.repositoryRoot,
      env: {
        ...where.environment,
        // `backend` first: whatever else the environment had cannot shadow `app`.
        PYTHONPATH: inherited ? `${backend}${delimiter}${inherited}` : backend,
      },
    },
  };
}

function installedResolution(where: Where, platform: NodeJS.Platform): Resolution {
  const resources = where.resourcesPath ?? "";
  const executable = installedPython(resources, platform);
  if (resources === "" || !where.exists(executable)) {
    return {
      ok: false,
      line:
        `A instalação está incompleta: falta ${executable}. ` +
        "Reinstale o SolidVision.",
    };
  }
  return {
    ok: true,
    command: {
      executable,
      args: LAUNCHER_ARGUMENTS,
      cwd: resources,
      env: { ...where.environment },
    },
  };
}

function developmentPython(
  where: Where,
  platform: NodeJS.Platform,
): { ok: true; path: string } | { ok: false; line: string } {
  const chosen = where.environment.SOLIDVISION_PYTHON;
  if (chosen) {
    // Chosen by name, so never replaced by another in silence.
    return where.exists(chosen)
      ? { ok: true, path: chosen }
      : {
          ok: false,
          line: `SOLIDVISION_PYTHON é ${chosen}, e esse arquivo não existe.`,
        };
  }
  const candidates = developmentCandidates(where.repositoryRoot, platform);
  const found = candidates.find((candidate) => where.exists(candidate));
  if (found !== undefined) {
    return { ok: true, path: found };
  }
  return {
    ok: false,
    line:
      `Nenhum Python do SolidVision foi encontrado: nem ${candidates.join(", nem ")}. ` +
      "Crie o ambiente virtual como o README diz, ou defina SOLIDVISION_PYTHON.",
  };
}
