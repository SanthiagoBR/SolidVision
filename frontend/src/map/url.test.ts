import { describe, expect, it } from "vitest";

import { NO_FILTERS } from "../search/params";
import {
  MAP_PATH,
  mapHref,
  mapHrefFor,
  mapQueryString,
  readMapUrl,
  roundView,
  viewKey,
} from "./url";

const D2 = "00000000-0000-0000-0000-0000000000d2";
const read = (query: string) => readMapUrl(new URLSearchParams(query));

describe("the URL of the map (RFC-040 section 5.1)", () => {
  it("is /mapa with nothing: all of Brazil", () => {
    expect(read("")).toEqual({ query: null, filters: NO_FILTERS, view: null });
    expect(mapHref({ query: null, filters: NO_FILTERS, view: null })).toBe(MAP_PATH);
  });

  it("reads and writes a framing, first", () => {
    const url = read("centro=-26.3214,-48.8163&zoom=9");

    expect(url.view).toEqual({ center: { lat: -26.3214, lon: -48.8163 }, zoom: 9 });
    expect(mapQueryString(url)).toBe("centro=-26.3214,-48.8163&zoom=9");
  });

  it("keeps the search and the filters, by the search's names", () => {
    const query =
      "centro=-26.3214,-48.8163&zoom=9&q=telhado%20azul&dispositivo=" +
      `${D2}&de=2018-01-01&ate=2018-12-31`;
    const url = read(query);

    expect(url.query).toEqual({ kind: "text", q: "telhado azul" });
    expect(url.filters.devices).toEqual([D2]);
    expect(url.filters.from).toBe("2018-01-01");
    expect(url.filters.to).toBe("2018-12-31");
    expect(mapQueryString(url)).toBe(query);
  });

  it("carries a 'parecidas' and a dropped photo as the search names them", () => {
    expect(read("parecidas=abc").query).toEqual({ kind: "similar", imageId: "abc" });
    expect(read("imagem").query).toEqual({ kind: "image" });
    expect(mapHref(read("imagem&de=2018-01-01"))).toBe("/mapa?imagem&de=2018-01-01");
  });

  it("leaves perto, raio, perto_de and foto out", () => {
    const url = read(
      "q=telhado&perto=-26.32141,-48.81631&raio=1000&perto_de=xyz&foto=abc",
    );

    expect(url.filters.near).toBeNull();
    expect(url.filters.radius).toBeNull();
    expect(url.filters.nearOf).toBeNull();
    expect(mapQueryString(url)).toBe("q=telhado");
  });

  it("never carries the listing by date: the balloon has its own button", () => {
    const url = read("zoom=9&centro=-26.3214,-48.8163&todas&perto=1,2&raio=1000");

    expect(url.query).toBeNull();
    expect(
      mapHrefFor(
        { kind: "browse", order: "oldest" },
        { ...NO_FILTERS, from: "2018-01-01" },
      ),
    ).toBe(`${MAP_PATH}?de=2018-01-01`);
  });

  it("ignores a framing that is not valid", () => {
    const invalid = [
      "centro=-26.3,-48.8",
      "zoom=9",
      "centro=-26.3,-48.8&zoom=",
      "centro=-26.3,-48.8&zoom=2",
      "centro=-26.3,-48.8&zoom=19",
      "centro=-26.3,-48.8&zoom=9.5",
      "centro=-96,-48.8&zoom=9",
      "centro=-26.3,-188&zoom=9",
      "centro=-26.3&zoom=9",
      "centro=-26.3,-48.8,1&zoom=9",
      "centro=sul,oeste&zoom=9",
      "centro=,&zoom=9",
    ];
    for (const query of invalid) {
      expect(read(query).view, query).toBeNull();
    }
    expect(read("centro=-26.3,-48.8&zoom=abc&q=x").query).toEqual({
      kind: "text",
      q: "x",
    });
  });

  it("rounds the centre to four places, as the URL keeps it", () => {
    const view = roundView({ lat: -26.321449, lon: -48.816312 }, 9);

    expect(view).toEqual({ center: { lat: -26.3214, lon: -48.8163 }, zoom: 9 });
    expect(viewKey(view)).toBe("-26.3214,-48.8163,9");
    expect(viewKey(null)).toBe("");
  });

  it("is where 'ou escolha um lugar no mapa' goes, with the search and its filters", () => {
    const href = mapHrefFor(
      { kind: "text", q: "lago" },
      {
        ...NO_FILTERS,
        devices: [D2],
        near: { lat: 1, lon: 2 },
        radius: 300,
        nearOf: "x",
      },
    );

    expect(href).toBe(`/mapa?q=lago&dispositivo=${D2}`);
    expect(mapHrefFor(null, NO_FILTERS)).toBe("/mapa");
  });
});
