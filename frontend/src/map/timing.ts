/**
 * One mark per drawing of the map, through the bridge (RFC-040 sections 5.6
 * and 8).
 *
 * From the gesture -- the click on "Mapa", the end of a drag or of a zoom that
 * asks again, a filter -- to the response, and to the frame after the cells
 * were drawn: two `requestAnimationFrame` after they entered the map, the
 * first of which is the one the canvas draws in. Measured in the page, with
 * `performance.now()`, as the marks of RFC-038 and RFC-039. The mark carries
 * the counts, the precisions and the times, and never the box nor the framing:
 * the area one looks at is the map's query (section 2.7).
 *
 * A gesture is completed only by a response that arrived after it. A response
 * from before it -- cells served from the cache -- ends it with no mark, so
 * that a later response, which no gesture asked for, is not timed from it.
 *
 * No Leaflet here: the header's "Mapa" starts the gesture before the screen's
 * part of the build has loaded.
 */

import { bridge } from "../shell/bridge";

let pending: number | null = null;

export function startMapGesture(): void {
  pending = performance.now();
}

export interface PaintedMap {
  readonly cells: number;
  readonly precisionRequested: number;
  readonly precisionApplied: number;
  /** `performance.now()` when the response arrived. */
  readonly receivedAt: number;
}

/** Called on the frame after a response's cells were drawn. */
export function finishMapGesture(map: PaintedMap, paintedAt: number): void {
  const startedAt = pending;
  if (startedAt === null) {
    return;
  }
  pending = null;
  if (map.receivedAt < startedAt) {
    return;
  }
  bridge()?.reportMap?.({
    cells: map.cells,
    precisionRequested: map.precisionRequested,
    precisionApplied: map.precisionApplied,
    responseMs: map.receivedAt - startedAt,
    paintedMs: paintedAt - startedAt,
  });
}
