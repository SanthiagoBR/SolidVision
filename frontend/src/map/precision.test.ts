import { describe, expect, it } from "vitest";

import {
  cellSizeText,
  cellStep,
  MAX_ZOOM,
  MIN_ZOOM,
  pixelsPerDegree,
  precisionForZoom,
} from "./precision";

describe("the precision of the zoom (RFC-040 section 5.3)", () => {
  it("is each row of the table", () => {
    const rows: [number[], number][] = [
      [[3, 4, 5, 6], 0],
      [[7, 8, 9, 10], 1],
      [[11, 12, 13], 2],
      [[14, 15, 16, 17, 18], 3],
    ];
    for (const [zooms, precision] of rows) {
      for (const zoom of zooms) {
        expect(precisionForZoom(zoom), `zoom ${zoom}`).toBe(precision);
      }
    }
  });

  it("gives every cell at least 8 px, but at zoom 3, where 0 is the floor", () => {
    for (let zoom = MIN_ZOOM; zoom <= MAX_ZOOM; zoom += 1) {
      const px = pixelsPerDegree(zoom) * cellStep(precisionForZoom(zoom));
      if (zoom === 3) {
        expect(Math.round(px)).toBe(6);
      } else {
        expect(px, `zoom ${zoom}`).toBeGreaterThanOrEqual(8);
      }
    }
    // The table's ends: 46 px at zoom 6, 9 at 7, 186 at 18.
    expect(Math.round(pixelsPerDegree(6))).toBe(46);
    expect(Math.round(pixelsPerDegree(7) / 10)).toBe(9);
    expect(Math.round(pixelsPerDegree(18) / 1_000)).toBe(186);
  });

  it("is never below 0 nor above 3", () => {
    expect(precisionForZoom(0)).toBe(0);
    expect(precisionForZoom(-2)).toBe(0);
    expect(precisionForZoom(22)).toBe(3);
  });

  it("names the cells by their order of magnitude", () => {
    expect([0, 1, 2, 3].map(cellSizeText)).toEqual([
      "~110 km",
      "~11 km",
      "~1 km",
      "~100 m",
    ]);
    expect(cellStep(0)).toBe(1);
    expect(cellStep(3)).toBeCloseTo(0.001, 12);
  });
});
