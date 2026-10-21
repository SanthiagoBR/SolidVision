/**
 * One mark per listing of a device's folders, through the bridge (RFC-039
 * sections 5.9 and 8).
 *
 * From the gesture -- the click on a folder, on a part of the trail, on a
 * card -- to the response, and to the frame after the list was drawn with it.
 * Measured in the page, with `performance.now()`, as the mark of a search
 * (RFC-038 section 5.6). The mark carries the count and the times, and never
 * the path.
 *
 * A gesture is completed only by the listing it asked for -- the same device
 * and the same canonical path -- and only by a response that arrived after it:
 * a level served from the cache while it is read again does not end it.
 */

import { bridge } from "../shell/bridge";

interface Gesture {
  readonly deviceId: string;
  readonly path: string;
  readonly startedAt: number;
}

let pending: Gesture | null = null;

export function startFoldersGesture(deviceId: string, path: string): void {
  pending = { deviceId, path, startedAt: performance.now() };
}

export interface PaintedLevel {
  readonly deviceId: string;
  readonly path: string;
  readonly folders: number;
  readonly receivedAt: number;
}

/** Called on the frame after a level was drawn. */
export function finishFoldersGesture(level: PaintedLevel, paintedAt: number): void {
  const gesture = pending;
  if (
    gesture === null ||
    gesture.deviceId !== level.deviceId ||
    gesture.path !== level.path ||
    level.receivedAt < gesture.startedAt
  ) {
    return;
  }
  pending = null;
  bridge()?.reportFolders?.({
    folders: level.folders,
    responseMs: level.receivedAt - gesture.startedAt,
    paintedMs: paintedAt - gesture.startedAt,
  });
}
