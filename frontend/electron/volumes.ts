/**
 * The volume messages of the window, and how they become one `devices-changed`
 * for the page (RFC-039 sections 2.4 and 5.9).
 *
 * Windows sends `WM_DEVICECHANGE` to every top-level window when a volume
 * arrives or leaves, with no registration: `DBT_DEVICEARRIVAL` and
 * `DBT_DEVICEREMOVECOMPLETE` in the `wParam`. `BrowserWindow.hookWindowMessage`
 * hands the `wParam` over as a `Buffer`, and the `lParam` as a pointer that
 * JavaScript cannot read: the type of the device -- a volume, a port -- is not
 * known here, and nothing is lost by that, because what follows is one
 * `GET /devices`, which says what changed.
 *
 *     wParam                       what the page gets
 *     0x8000 DBT_DEVICEARRIVAL     a devices-changed, grouped
 *     0x8004 DBT_DEVICEREMOVE...   a devices-changed, grouped
 *     anything else                nothing (0x0007 DBT_DEVNODES_CHANGED comes
 *                                  for every device node, a mouse included)
 *
 * **Grouped in 250 ms**: a disk can carry more than one volume, and a burst of
 * messages is one change of the sidebar. The first message starts the window;
 * those inside it join it; at its end the page is told once. The 250 is a
 * choice.
 *
 * No Electron here, as in `events.ts`: the timer is given, and the tests drive
 * it.
 */

export const WM_DEVICECHANGE = 0x0219;
export const DBT_DEVICEARRIVAL = 0x8000;
export const DBT_DEVICEREMOVECOMPLETE = 0x8004;
export const GROUP_MS = 250;

export type VolumeChange = "arrival" | "removal";

/** What one burst of messages was. */
export interface VolumeChanges {
  readonly arrivals: number;
  readonly removals: number;
}

/** The change a `WM_DEVICECHANGE` announces, from its `wParam`; `null` for the
 * ones the page has no use for. The low 32 bits, little-endian: a `WPARAM` is
 * 8 bytes on a 64-bit Windows, and the event codes fit in the first 2. */
export function volumeChangeOf(wParam: Uint8Array | number): VolumeChange | null {
  let code: number;
  if (typeof wParam === "number") {
    code = wParam;
  } else {
    if (wParam.byteLength < 4) {
      return null;
    }
    code = new DataView(wParam.buffer, wParam.byteOffset, 4).getUint32(0, true);
  }
  if (code === DBT_DEVICEARRIVAL) {
    return "arrival";
  }
  if (code === DBT_DEVICEREMOVECOMPLETE) {
    return "removal";
  }
  return null;
}

export type Schedule = (callback: () => void, afterMs: number) => void;

/** Collects the messages of one burst and reports them once, `GROUP_MS` after
 * the first. */
export class VolumeMessages {
  private pending: { arrivals: number; removals: number } | null = null;

  constructor(
    private readonly schedule: Schedule,
    private readonly report: (changes: VolumeChanges) => void,
    private readonly groupMs: number = GROUP_MS,
  ) {}

  /** One `WM_DEVICECHANGE`. True when it was a volume arriving or leaving. */
  receive(wParam: Uint8Array | number): boolean {
    const change = volumeChangeOf(wParam);
    if (change === null) {
      return false;
    }
    if (this.pending === null) {
      this.pending = { arrivals: 0, removals: 0 };
      this.schedule(() => this.flush(), this.groupMs);
    }
    if (change === "arrival") {
      this.pending.arrivals += 1;
    } else {
      this.pending.removals += 1;
    }
    return true;
  }

  private flush(): void {
    const changes = this.pending;
    this.pending = null;
    if (changes !== null) {
      this.report(changes);
    }
  }
}

/** The line of `shell.log`: how many, and never which letter -- it is not
 * known here, and the page asks the API. */
export function volumeChangesLine(changes: VolumeChanges): string {
  return (
    `Volumes changed (WM_DEVICECHANGE): ${changes.arrivals} arrived, ` +
    `${changes.removals} removed; the page asks GET /devices`
  );
}
