/**
 * The cells of a response (RFC-040 sections 5.3 to 5.5).
 *
 * **A cell is a square around its centre.** The centre is the photos'
 * coordinate rounded to `precision_applied` places (`map_schema.py`), so the
 * square goes from the centre minus half a step to the centre plus half a
 * step, on both axes.
 *
 * **Five fixed classes, by order of magnitude**: "1", "2 a 9", "10 a 99",
 * "100 a 999" and "1.000 ou mais" photos per cell. Fixed, and not quantiles:
 * a cell changes colour only when it changes.
 *
 * **The radius of "Buscar perto daqui" covers the whole cell**: the smallest
 * of the search's list, from `min_radius_m` up, that reaches the farthest of
 * the four corners from the centre, by the haversine. A circle that did not
 * cover the square would leave out photos the balloon counted.
 *
 * **The list** is the 20 fullest cells of the visible area, from the fullest
 * down, in a stable order: the keyboard's and the screen reader's way to a
 * cell, because the canvas gives no cell the focus. The 20 is a choice.
 */

import type { Point } from "../search/params";
import { radiusOptions } from "../search/radius";
import { type Box, intersects } from "./box";
import { cellStep } from "./precision";

export interface Cell {
  readonly latitude: number;
  readonly longitude: number;
  readonly count: number;
}

/** The square of a cell, cut at the poles. */
export function cellBox(cell: Cell, precision: number): Box {
  const half = cellStep(precision) / 2;
  return {
    south: Math.max(-90, cell.latitude - half),
    west: cell.longitude - half,
    north: Math.min(90, cell.latitude + half),
    east: cell.longitude + half,
  };
}

export const CLASS_LABELS = [
  "1",
  "2 a 9",
  "10 a 99",
  "100 a 999",
  "1.000 ou mais",
] as const;

/** The class of a count, 0 to 4: one per order of magnitude, the last open. */
export function countClass(count: number): number {
  if (count < 2) {
    return 0;
  }
  if (count < 10) {
    return 1;
  }
  if (count < 100) {
    return 2;
  }
  if (count < 1_000) {
    return 3;
  }
  return 4;
}

/** The IUGG mean radius, the backend's `EARTH_RADIUS_M` (`haversine.py`). */
export const EARTH_RADIUS_M = 6_371_008.8;

const radians = (degrees: number) => (degrees * Math.PI) / 180;
const squaredSineOfHalf = (angle: number) => Math.sin(angle / 2) * Math.sin(angle / 2);

/** The great-circle distance, in metres, as the backend computes it. */
export function haversineM(first: Point, second: Point): number {
  const h =
    squaredSineOfHalf(radians(second.lat - first.lat)) +
    Math.cos(radians(first.lat)) *
      Math.cos(radians(second.lat)) *
      squaredSineOfHalf(radians(second.lon - first.lon));
  return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(h)));
}

/** From the centre to the farthest corner: the corner nearer the equator,
 * where a degree of longitude is longer. */
export function cornerDistanceM(cell: Cell, precision: number): number {
  const box = cellBox(cell, precision);
  const centre = { lat: cell.latitude, lon: cell.longitude };
  return Math.max(
    ...[box.south, box.north].flatMap((lat) =>
      [box.west, box.east].map((lon) => haversineM(centre, { lat, lon })),
    ),
  );
}

/** The smallest radius of the search's list that covers the cell; the
 * largest of the list when none does. */
export function cellRadius(cell: Cell, precision: number, minRadiusM: number): number {
  const reach = cornerDistanceM(cell, precision);
  const options = radiusOptions(minRadiusM);
  return options.find((radius) => radius >= reach) ?? options[options.length - 1] ?? 0;
}

/** The cells whose square meets the visible area. */
export function visibleCells(
  cells: readonly Cell[],
  precision: number,
  visible: Box,
): Cell[] {
  return cells.filter((cell) => intersects(cellBox(cell, precision), visible));
}

export const LIST_SIZE = 20;

/** The fullest cells of the visible area, from the fullest down; a tie goes
 * north first, then west first, so the order never depends on the response's. */
export function fullestCells(
  cells: readonly Cell[],
  precision: number,
  visible: Box,
  size: number = LIST_SIZE,
): Cell[] {
  return visibleCells(cells, precision, visible)
    .sort(
      (first, second) =>
        second.count - first.count ||
        second.latitude - first.latitude ||
        first.longitude - second.longitude,
    )
    .slice(0, size);
}
