import createClient from "openapi-fetch";
import { describe, expect, it } from "vitest";

import type { paths } from "../api/schema";
import {
  apiQuery,
  devicePillText,
  NO_FILTERS,
  readSearchUrl,
  searchHref,
  searchQueryString,
  toggleDevice,
  type Filters,
} from "./params";

const read = (query: string) => readSearchUrl(new URLSearchParams(query));

describe("the URL of the screen", () => {
  it("reads the three kinds of search", () => {
    expect(read("q=telhado+azul").query).toEqual({ kind: "text", q: "telhado azul" });
    expect(read("parecidas=abc").query).toEqual({ kind: "similar", imageId: "abc" });
    expect(read("imagem").query).toEqual({ kind: "image" });
    expect(read("").query).toBeNull();
  });

  it("reads the filters with the interface's names", () => {
    const url = read(
      "q=x&dispositivo=A&dispositivo=B&de=2018-01-01&ate=2018-12-31" +
        "&perto=-26.32141,-48.81631&raio=5000&perto_de=img&foto=other",
    );

    expect(url.filters).toEqual({
      devices: ["A", "B"],
      from: "2018-01-01",
      to: "2018-12-31",
      near: { lat: -26.32141, lon: -48.81631 },
      radius: 5000,
      nearOf: "img",
    });
    expect(url.photo).toBe("other");
  });

  it("ignores what is not a date, a point or a radius", () => {
    const url = read("q=x&de=31/12/2018&ate=2018-02-30&perto=-26&raio=-3");

    expect(url.filters).toEqual(NO_FILTERS);
  });

  it("writes back what it reads, and `imagem` without a value", () => {
    const query =
      "q=telhado%20azul&dispositivo=A&dispositivo=B&de=2018-01-01&ate=2018-12-31" +
      "&perto=-26.32141%2C-48.81631&raio=5000&perto_de=img&foto=other";

    expect(searchQueryString(read(query))).toBe(query);
    expect(searchHref(read("imagem&de=2018-01-01"))).toBe(
      "/busca?imagem&de=2018-01-01",
    );
  });

  it("goes home, with its filters, when there is no search", () => {
    expect(searchHref(read("dispositivo=A"))).toBe("/?dispositivo=A");
    expect(searchHref(read(""))).toBe("/");
  });
});

describe("apiQuery", () => {
  it("sends nothing but the limit without filters", () => {
    expect(apiQuery(NO_FILTERS, 40, 300)).toEqual({ limit: 40 });
  });

  it("translates every filter to the API's names", () => {
    const filters: Filters = {
      devices: ["A", "B"],
      from: "2018-01-01",
      to: "2018-12-31",
      near: { lat: -26.3, lon: -48.8 },
      radius: 5000,
      nearOf: "img",
    };

    expect(apiQuery(filters, 100, 300)).toEqual({
      limit: 100,
      device_id: ["A", "B"],
      captured_from: "2018-01-01",
      captured_to: "2019-01-01",
      near_lat: -26.3,
      near_lon: -48.8,
      radius_m: 5000,
    });
  });

  it("sends the day after the last one: the API's end is exclusive", () => {
    const query = apiQuery({ ...NO_FILTERS, to: "2020-02-28" }, 40, 300);

    expect(query.captured_to).toBe("2020-02-29");
    expect(query.captured_from).toBeUndefined();
  });

  it("puts no Z, no offset and no time in any parameter", () => {
    const query = apiQuery(
      { ...NO_FILTERS, from: "2018-03-11", to: "2018-11-04" },
      40,
      300,
    );

    for (const value of [query.captured_from, query.captured_to]) {
      expect(value).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    }
    expect(JSON.stringify(query)).not.toMatch(/Z|T\d|[+-]\d\d:\d\d/);
  });

  it("does not send an until before the from", () => {
    const query = apiQuery(
      { ...NO_FILTERS, from: "2018-12-31", to: "2018-01-01" },
      40,
      300,
    );

    expect(query.captured_from).toBe("2018-12-31");
    expect(query.captured_to).toBeUndefined();
  });

  it("sends the radius on the screen: 1 km when nobody chose, never below the floor", () => {
    const near = { ...NO_FILTERS, near: { lat: 1, lon: 2 } };

    expect(apiQuery(near, 40, 300).radius_m).toBe(1000);
    expect(apiQuery({ ...near, radius: 200 }, 40, 300).radius_m).toBe(1000);
    expect(apiQuery({ ...near, radius: 300 }, 40, 300).radius_m).toBe(300);
  });

  it("is serialized by openapi-fetch as a repeated device_id", async () => {
    let requested = "";
    const client = createClient<paths>({
      baseUrl: "http://127.0.0.1",
      fetch: async (request: Request) => {
        requested = request.url;
        return new Response("{}", {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
    });
    const query = apiQuery(
      { ...NO_FILTERS, devices: ["A", "B"], from: "2018-01-01" },
      40,
      300,
    );

    await client.GET("/api/v1/images/search", {
      params: { query: { q: "x", ...query } },
    });

    const search = new URL(requested).searchParams;
    expect(search.getAll("device_id")).toEqual(["A", "B"]);
    expect(new URL(requested).search).toContain("device_id=A&device_id=B");
    expect(search.get("captured_from")).toBe("2018-01-01");
    expect(search.has("captured_to")).toBe(false);
  });
});

describe("the devices", () => {
  const devices = [
    { id: "1", label: "HD1" },
    { id: "2", label: "HD2" },
    { id: "3", label: "HD3" },
    { id: "4", label: "HD4" },
  ];
  const all = devices.map((device) => device.id);

  it("names the selection in the pill", () => {
    expect(devicePillText(devices, [])).toBe("Dispositivos · todos (4)");
    expect(devicePillText(devices, all)).toBe("Dispositivos · todos (4)");
    expect(devicePillText(devices, ["3"])).toBe("Dispositivos · HD3");
    expect(devicePillText(devices, ["1", "3"])).toBe("Dispositivos · 2 de 4");
    expect(devicePillText(null, [])).toBe("Dispositivos · todos");
  });

  it("checking the last one is the same as none: all, with no device_id", () => {
    expect(toggleDevice([], "1", all)).toEqual(["1"]);
    expect(toggleDevice(["1"], "1", all)).toEqual([]);
    expect(toggleDevice(["1", "2", "3"], "4", all)).toEqual([]);
    expect(
      apiQuery(
        { ...NO_FILTERS, devices: toggleDevice(["1", "2", "3"], "4", all) },
        40,
        300,
      ),
    ).toEqual({ limit: 40 });
  });
});
