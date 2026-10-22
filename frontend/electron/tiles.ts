/**
 * The requests to the OpenStreetMap's tile host (RFC-040 sections 2.6 and 5.6).
 *
 * The tile usage policy, read on 2026-10-22, asks for a User-Agent that names
 * the application and does not imitate a browser. Electron's is Chromium's,
 * with the app's name and Electron's in the middle. **The window sends its own
 * to the tile host, and only there**: `SolidVision/<version> (+<project>)`,
 * with the version of `package.json`, the same in development and installed,
 * because the policy asks for a stable one. Every other request of the page
 * keeps the default, and the `Referer` stays as Chromium sends it: the page's
 * origin.
 *
 * No Electron here: `main.ts` hands `session.webRequest` this filter and this
 * function.
 */

export const TILE_URL_PATTERNS: string[] = ["https://tile.openstreetmap.org/*"];
export const PROJECT_URL = "https://github.com/SanthiagoBR/SolidVision";

export function tileUserAgent(version: string): string {
  return `SolidVision/${version} (+${PROJECT_URL})`;
}

export function isTileRequest(target: string): boolean {
  try {
    const url = new URL(target);
    return url.protocol === "https:" && url.host === "tile.openstreetmap.org";
  } catch {
    return false;
  }
}

/** The headers of a request, with the window's User-Agent when it goes to the
 * tile host, and untouched otherwise. */
export function withTileUserAgent(
  target: string,
  headers: Record<string, string>,
  version: string,
): Record<string, string> {
  if (!isTileRequest(target)) {
    return headers;
  }
  const kept = Object.fromEntries(
    Object.entries(headers).filter(([name]) => name.toLowerCase() !== "user-agent"),
  );
  return { ...kept, "User-Agent": tileUserAgent(version) };
}
