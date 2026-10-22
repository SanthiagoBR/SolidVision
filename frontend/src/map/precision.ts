/**
 * The precision a zoom asks for, and the size of its cells (RFC-040 sections
 * 2.5 and 5.3).
 *
 * The route groups the positions by rounding them to `precision` decimal places
 * of a degree: a cell is a square of 10^-precision degrees. **The page asks for
 * the largest precision from 0 to 3 whose cell is at least 8 px wide on the
 * screen**, and never below 0. In the Web Mercator a degree of longitude is
 * `256 × 2^zoom / 360` px at every latitude, so the width is the side that
 * binds:
 *
 *     zoom      precision   the cell   on the screen
 *     3 to 6    0           ~110 km    6 to 46 px
 *     7 to 10   1           ~11 km     9 to 73 px
 *     11 to 13  2           ~1 km      15 to 58 px
 *     14 to 18  3           ~100 m     12 to 186 px
 *
 * **The ceiling is 3**: below ~100 m a cell would claim a precision the
 * position does not have, because it is the aircraft's, tens or hundreds of
 * metres from the photographed ground (RFC-032 section 2.2). The 8 px is a
 * choice.
 *
 * The cells are drawn at the `precision_applied` of the response, which the
 * route lowers when the area has more than `MAX_MAP_CELLS` of them.
 */

export const MIN_ZOOM = 3;
export const MAX_ZOOM = 18;
export const MAX_CELL_PRECISION = 3;
export const MIN_CELL_PX = 8;

/** Pixels of one degree of longitude, at any latitude, in the Web Mercator. */
export function pixelsPerDegree(zoom: number): number {
  return (256 * 2 ** zoom) / 360;
}

/** The largest precision from 0 to 3 whose cell has at least 8 px. */
export function precisionForZoom(zoom: number): number {
  const fits = Math.floor(Math.log10(pixelsPerDegree(zoom) / MIN_CELL_PX));
  return Math.min(MAX_CELL_PRECISION, Math.max(0, fits));
}

/** The side of a cell, in degrees: 1 at precision 0, 0.001 at precision 3. */
export function cellStep(precision: number): number {
  return 10 ** -precision;
}

/** `~110 km`, `~11 km`, `~1 km`, `~100 m`: a degree is about 111 km, and the
 * names are of the order of magnitude, as the RFC writes them. */
export function cellSizeText(precision: number): string {
  switch (precision) {
    case 0:
      return "~110 km";
    case 1:
      return "~11 km";
    case 2:
      return "~1 km";
    case 3:
      return "~100 m";
    case 4:
      return "~10 m";
    case 5:
      return "~1 m";
    default:
      return precision < 0 ? "~110 km" : "~0,1 m";
  }
}
