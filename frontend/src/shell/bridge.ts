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

/** The job runner as the window last heard of it (RFC-039 section 5.8):
 * `running`, `restarting` or `unavailable`, with the line it left. */
export interface IndexingStatus {
  readonly state: string;
  readonly detail: string | null;
}

/** One listing of a device's folders, painted: the mark of RFC-039 section
 * 5.9. The count and the times, and never the path. */
export interface FoldersReport {
  readonly folders: number;
  /** From the gesture to the response. */
  readonly responseMs: number;
  /** From the gesture to the frame after the list was drawn. */
  readonly paintedMs: number;
}

export interface Bridge {
  onLauncherEvent(callback: (event: LauncherEventMessage) => void): () => void;
  openLogsFolder(): void;
  reportHome(report: HomeReport): void;
  /** Optional: a window of RFC-037 has no such channel. */
  reportSearch?(report: SearchReport): void;
  /** A volume arrived or left (RFC-039 section 5.9). Optional, as the two
   * below: a window of RFC-038 has none of them. */
  onDevicesChanged?(callback: () => void): () => void;
  /** The job runner's last state; `null` from a window that does not own the
   * launcher, which hears nothing of it. */
  indexing?(): Promise<IndexingStatus | null>;
  reportFolders?(report: FoldersReport): void;
}

declare global {
  interface Window {
    solidvision?: Bridge;
  }
}

export function bridge(): Bridge | null {
  return window.solidvision ?? null;
}
