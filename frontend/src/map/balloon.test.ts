// Where "Buscar perto daqui" of a cell goes (RFC-040 section 5.4): the search
// the map carries, with its filters and the cell's circle, and no `perto_de`.

import { describe, expect, it } from "vitest";

import { nearPointSearch } from "../search/actions";
import {
  NO_FILTERS,
  readSearchUrl,
  searchHref,
  type SearchUrl,
} from "../search/params";

const D2 = "00000000-0000-0000-0000-0000000000d2";
const CENTRE = { lat: -26.32, lon: -48.82 };
const filters = { ...NO_FILTERS, devices: [D2], from: "2018-01-01", to: "2018-12-31" };
const fromMap = (query: SearchUrl["query"]): SearchUrl => ({
  query,
  filters,
  photo: null,
});
const target = (query: SearchUrl["query"]) =>
  searchHref(nearPointSearch(fromMap(query), CENTRE, 1_000));

describe("the target of 'Buscar perto daqui' of a cell", () => {
  it("is the search by text, with the filters and the circle", () => {
    expect(target({ kind: "text", q: "telhado azul" })).toBe(
      `/busca?q=telhado%20azul&dispositivo=${D2}&de=2018-01-01&ate=2018-12-31` +
        "&perto=-26.32%2C-48.82&raio=1000",
    );
  });

  it("is 'parecidas', and the dropped photo, the same way", () => {
    expect(target({ kind: "similar", imageId: "abc" })).toMatch(
      /^\/busca\?parecidas=abc&dispositivo=.*&perto=-26\.32%2C-48\.82&raio=1000$/,
    );
    expect(target({ kind: "image" })).toMatch(
      /^\/busca\?imagem&dispositivo=.*&raio=1000$/,
    );
  });

  it("is the home screen with the circle, without a search", () => {
    expect(target(null)).toBe(
      `/?dispositivo=${D2}&de=2018-01-01&ate=2018-12-31&perto=-26.32%2C-48.82&raio=1000`,
    );
  });

  it("names no photo, and the search reads it back with the circle", () => {
    for (const query of [null, { kind: "text", q: "x" } as const]) {
      const href = target(query);
      const url = readSearchUrl(new URLSearchParams(href.split("?")[1]));

      expect(href).not.toContain("perto_de");
      expect(url.filters.near).toEqual(CENTRE);
      expect(url.filters.radius).toBe(1_000);
      expect(url.filters.nearOf).toBeNull();
      expect(url.filters.devices).toEqual([D2]);
    }
  });

  it("drops a circle the search had, for the cell's", () => {
    const before = fromMap({ kind: "text", q: "x" });
    const withCircle = {
      ...before,
      filters: { ...filters, near: { lat: 1, lon: 2 }, radius: 300, nearOf: "y" },
    };

    expect(nearPointSearch(withCircle, CENTRE, 10_000).filters).toMatchObject({
      near: CENTRE,
      radius: 10_000,
      nearOf: null,
    });
  });
});
