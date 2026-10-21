/**
 * One mark per search, through the bridge (RFC-038 sections 5.6 and 13).
 *
 * From the gesture -- the Enter, the click on "Mostrar mais", the photo chosen
 * -- to the response, and to the frame after the last thumbnail of the
 * response was decoded, or became the frame of a 404. Measured in the page,
 * with `performance.now()`. The mark carries the kind and the counts, and
 * never the query.
 *
 * A gesture is completed only by a response that arrived after it: a page
 * served from the cache while the refetch runs does not end the measurement.
 */

import { bridge } from "../shell/bridge";
import type { SearchKind } from "./params";

interface Gesture {
  readonly kind: SearchKind;
  readonly limit: number;
  readonly startedAt: number;
}

let pending: Gesture | null = null;

export function startGesture(kind: SearchKind, limit: number): void {
  pending = { kind, limit, startedAt: performance.now() };
}

export interface PaintedPage {
  readonly kind: SearchKind;
  readonly limit: number;
  readonly results: number;
  readonly receivedAt: number;
}

/** Called on the frame after a page's thumbnails all settled. */
export function finishGesture(page: PaintedPage, paintedAt: number): void {
  const gesture = pending;
  if (
    gesture === null ||
    gesture.kind !== page.kind ||
    gesture.limit !== page.limit ||
    page.receivedAt < gesture.startedAt
  ) {
    return;
  }
  pending = null;
  bridge()?.reportSearch?.({
    kind: page.kind,
    limit: page.limit,
    results: page.results,
    responseMs: page.receivedAt - gesture.startedAt,
    paintedMs: paintedAt - gesture.startedAt,
  });
}
