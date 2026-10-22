import { describe, expect, it } from "vitest";

import { NO_FILTERS } from "../search/params";
import {
  apiMapQuery,
  type Box,
  clampBox,
  containsBox,
  mapFilters,
  MARGIN,
  planRequest,
  requestBox,
} from "./box";

const JOINVILLE: Box = { south: -26.5, west: -49.0, north: -26.1, east: -48.6 };

describe("the box asked for (RFC-040 sections 2.5 and 5.3)", () => {
  it("is the visible area with a quarter of it on each side", () => {
    const box = requestBox(JOINVILLE);

    expect(MARGIN).toBe(0.25);
    expect(box.south).toBeCloseTo(-26.6, 4);
    expect(box.north).toBeCloseTo(-26.0, 4);
    expect(box.west).toBeCloseTo(-49.1, 4);
    expect(box.east).toBeCloseTo(-48.5, 4);
    expect(containsBox(box, JOINVILLE)).toBe(true);
  });

  it("is rounded outwards to four places, and still holds the area", () => {
    const visible = { south: -26.12345, west: -48.98765, north: -26.0001, east: -48.6 };
    const box = requestBox(visible);

    for (const edge of [box.south, box.west, box.north, box.east]) {
      expect(Math.round(edge * 10_000) / 10_000).toBe(edge);
    }
    expect(containsBox(box, visible)).toBe(true);
  });

  it("is cut to the API's bands", () => {
    const world = requestBox({ south: -80, west: -170, north: 85, east: 175 });

    expect(world).toEqual({ south: -90, west: -180, north: 90, east: 180 });
  });

  it("is never inverted", () => {
    expect(clampBox({ south: 10, west: 20, north: -10, east: -20 })).toEqual({
      south: -10,
      west: -20,
      north: 10,
      east: 20,
    });
    const box = requestBox({ south: 89, west: 179, north: 95, east: 200 });
    expect(box.south).toBeLessThanOrEqual(box.north);
    expect(box.west).toBeLessThanOrEqual(box.east);
  });
});

describe("when the page asks again", () => {
  const first = planRequest(null, JOINVILLE, 10, {});

  it("asks when the screen opens", () => {
    expect(first.precision).toBe(1);
    expect(first.box).toEqual(requestBox(JOINVILLE));
  });

  it("asks nothing for an area inside the last box, at the same precision and filters", () => {
    const shortDrag = { south: -26.55, west: -49.05, north: -26.15, east: -48.65 };

    expect(planRequest(first, shortDrag, 10, {})).toBe(first);
    expect(planRequest(first, JOINVILLE, 9, {})).toBe(first);
  });

  it("asks when the area leaves the box", () => {
    const away = { south: -27.5, west: -49.0, north: -27.1, east: -48.6 };
    const next = planRequest(first, away, 10, {});

    expect(next).not.toBe(first);
    expect(next.box).toEqual(requestBox(away));
  });

  it("asks when the precision changes", () => {
    const next = planRequest(first, JOINVILLE, 11, {});

    expect(next).not.toBe(first);
    expect(next.precision).toBe(2);
  });

  it("asks when a filter changes", () => {
    const next = planRequest(first, JOINVILLE, 10, { captured_from: "2018-01-01" });

    expect(next).not.toBe(first);
    expect(next.filters).toEqual({ captured_from: "2018-01-01" });
  });
});

describe("the filters of the map (section 5.5)", () => {
  const D2 = "00000000-0000-0000-0000-0000000000d2";

  it("are the search's, with the API's names, without the circle nor the page", () => {
    const filters = mapFilters({
      ...NO_FILTERS,
      devices: [D2],
      from: "2018-01-01",
      to: "2018-12-31",
      near: { lat: -26.3, lon: -48.8 },
      radius: 1_000,
      nearOf: "x",
    });

    expect(filters).toEqual({
      device_id: [D2],
      captured_from: "2018-01-01",
      captured_to: "2019-01-01",
    });
  });

  it("send nothing a filter did not set", () => {
    expect(mapFilters(NO_FILTERS)).toEqual({});
  });

  it("go to the API with the box and the precision", () => {
    const query = apiMapQuery({
      box: { south: -27, west: -50, north: -26, east: -48 },
      precision: 2,
      filters: { device_id: [D2] },
    });

    expect(query).toEqual({
      min_lat: -27,
      min_lon: -50,
      max_lat: -26,
      max_lon: -48,
      precision: 2,
      device_id: [D2],
    });
  });
});
