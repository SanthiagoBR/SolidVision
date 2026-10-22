import { describe, expect, it } from "vitest";

import { radiusOptions } from "../search/radius";
import {
  type Cell,
  cellBox,
  cellRadius,
  CLASS_LABELS,
  cornerDistanceM,
  countClass,
  fullestCells,
  haversineM,
  LIST_SIZE,
  visibleCells,
} from "./cells";

const cell = (latitude: number, longitude: number, count = 1): Cell => ({
  latitude,
  longitude,
  count,
});

describe("the square of a cell (RFC-040 section 5.3)", () => {
  it("goes half a step each way from the centre, at precision 0", () => {
    const box = cellBox(cell(-26, -49), 0);

    expect(box).toEqual({ south: -26.5, west: -49.5, north: -25.5, east: -48.5 });
  });

  it("and at precision 3", () => {
    const box = cellBox(cell(-26.321, -48.816), 3);

    expect(box.south).toBeCloseTo(-26.3215, 9);
    expect(box.north).toBeCloseTo(-26.3205, 9);
    expect(box.west).toBeCloseTo(-48.8165, 9);
    expect(box.east).toBeCloseTo(-48.8155, 9);
  });

  it("is cut at the poles", () => {
    expect(cellBox(cell(90, 0), 0).north).toBe(90);
    expect(cellBox(cell(-90, 0), 0).south).toBe(-90);
  });
});

describe("the five classes", () => {
  it("are fixed, by order of magnitude, at their edges", () => {
    const edges: [number, number][] = [
      [1, 0],
      [2, 1],
      [9, 1],
      [10, 2],
      [99, 2],
      [100, 3],
      [999, 3],
      [1_000, 4],
      [123_456, 4],
    ];
    for (const [count, expected] of edges) {
      expect(countClass(count), `${count}`).toBe(expected);
    }
    expect(CLASS_LABELS).toEqual([
      "1",
      "2 a 9",
      "10 a 99",
      "100 a 999",
      "1.000 ou mais",
    ]);
  });
});

describe("the radius of a cell (section 5.4)", () => {
  it("measures as the backend's haversine does", () => {
    // One degree of latitude on the IUGG mean sphere.
    expect(haversineM({ lat: 0, lon: 0 }, { lat: 1, lon: 0 })).toBeCloseTo(111_195, 0);
  });

  it("is each row of the table, near 26° S", () => {
    const rows: [Cell, number, number, number][] = [
      // the cell, its precision, the distance to the corner, the radius
      [cell(-26.321, -48.816), 3, 75, 300],
      [cell(-26.32, -48.82), 2, 750, 1_000],
      [cell(-26.3, -48.8), 1, 7_500, 10_000],
      [cell(-26, -49), 0, 75_000, 100_000],
    ];
    for (const [square, precision, reach, radius] of rows) {
      expect(cornerDistanceM(square, precision) / reach).toBeCloseTo(1, 1);
      expect(cellRadius(square, precision, 300), `precision ${precision}`).toBe(radius);
    }
  });

  it("and at the equator, where a cell is widest", () => {
    expect(cellRadius(cell(0, -50), 0, 300)).toBe(100_000);
    expect(cellRadius(cell(0, -50.1), 1, 300)).toBe(10_000);
    expect(cellRadius(cell(0, -50.01), 2, 300)).toBe(1_000);
    expect(cellRadius(cell(0, -50.001), 3, 300)).toBe(300);
    expect(cornerDistanceM(cell(0, -50), 0)).toBeLessThan(100_000);
  });

  it("covers a state's cell with 10 km, where 5 km does not reach and 25 would", () => {
    const reach = cornerDistanceM(cell(-26.3, -48.8), 1);

    expect(reach).toBeGreaterThan(5_000);
    expect(reach).toBeLessThan(10_000);
    expect(radiusOptions(300)).toContain(10_000);
  });

  it("is the min_radius_m when the floor is above the cell", () => {
    expect(cellRadius(cell(-26.32, -48.82), 2, 2_000)).toBe(2_000);
    expect(cellRadius(cell(-26.321, -48.816), 3, 450)).toBe(450);
  });

  it("is the largest of the list when none reaches", () => {
    expect(cellRadius(cell(0, 0), 0, 300)).toBeLessThanOrEqual(100_000);
    expect(cellRadius(cell(-26, -49), -1, 300)).toBe(100_000);
  });
});

describe("the list (section 5.5)", () => {
  const visible = { south: -27, west: -50, north: -25, east: -48 };

  it("has the cells that meet the visible area, and none outside it", () => {
    const cells = [cell(-26, -49, 5), cell(-24, -49, 9), cell(-27.4, -49, 3)];

    expect(visibleCells(cells, 0, visible)).toEqual([
      cell(-26, -49, 5),
      cell(-27.4, -49, 3),
    ]);
  });

  it("is the 20 fullest, from the fullest, in a stable order", () => {
    const cells: Cell[] = [];
    for (let index = 0; index < 30; index += 1) {
      cells.push(
        cell(-26 - (index % 6) * 0.1, -49 + Math.floor(index / 6) * 0.1, index % 7),
      );
    }
    cells.push(cell(-10, -40, 1_000));
    const listed = fullestCells(cells, 1, visible);

    expect(LIST_SIZE).toBe(20);
    expect(listed).toHaveLength(20);
    expect(listed.some((square) => square.count === 1_000)).toBe(false);
    for (let index = 1; index < listed.length; index += 1) {
      const [before, after] = [listed[index - 1], listed[index]];
      if (before === undefined || after === undefined) {
        throw new Error("a hole in the list");
      }
      expect(before.count).toBeGreaterThanOrEqual(after.count);
      if (before.count === after.count) {
        expect(
          before.latitude > after.latitude ||
            (before.latitude === after.latitude && before.longitude < after.longitude),
        ).toBe(true);
      }
    }
    expect(fullestCells([...cells].reverse(), 1, visible)).toEqual(listed);
  });
});
