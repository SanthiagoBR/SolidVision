/**
 * The window's bridge, as the page sees it (RFC-037 section 6.1; `preload.ts`).
 *
 * Absent in a browser -- `npm run dev:web` -- and the interface works without
 * it: every use goes through `bridge()`, which may be `null`.
 */

/** The `child` and `address` events of the launcher (RFC-036 section 4.4). */
export interface LauncherEventMessage {
  readonly event: string;
  readonly name?: string;
  readonly state?: string;
  readonly detail?: string | null;
  readonly url?: string;
}

/** The numbers the home screen was painted with: the mark of section 6.5. */
export interface HomeReport {
  readonly devices: number;
  readonly indexed: number;
  readonly lastIndexedAt: string | null;
}

/** One search, painted: the mark of RFC-038 section 5.6. The kind and the
 * counts, and never the query. */
export interface SearchReport {
  readonly kind: "text" | "similar" | "image";
  readonly limit: number;
  readonly results: number;
  /** From the gesture to the response. */
  readonly responseMs: number;
  /** From the gesture to the frame after the last thumbnail. */
  readonly paintedMs: number;
}

export interface Bridge {
  onLauncherEvent(callback: (event: LauncherEventMessage) => void): () => void;
  openLogsFolder(): void;
  reportHome(report: HomeReport): void;
  /** Optional: a window of RFC-037 has no such channel. */
  reportSearch?(report: SearchReport): void;
}

declare global {
  interface Window {
    solidvision?: Bridge;
  }
}

export function bridge(): Bridge | null {
  return window.solidvision ?? null;
}
