/**
 * The listing by date of RFC-044, in Node and with no DOM: its URL, what it
 * sends, how its pages add up on the grid, the corner and the divider, the
 * header, the three doors to it, and its messages (section 8).
 */

import createClient from "openapi-fetch";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { ImageDetail } from "../api/client";
import type { paths } from "../api/schema";
import { browseSearch, nearPointBrowse, PAGE_SIZE } from "./actions";
import type { BrowseSummary } from "./api";
import {
  BROWSE_ACTION_TEXT,
  browseHeaderText,
  browsePageLimit,
  canBrowseMore,
  cardDateText,
  dateCorner,
  DIVIDER_NOTE,
  dividerTitle,
  emptyBrowseText,
  firstUndatedId,
  flattenPages,
  progressText,
} from "./browse";
import { BAD_FILTERS, errorMessage, LIST_CHANGED, NO_ANSWER } from "./messages";
import {
  apiBrowseQuery,
  NO_FILTERS,
  readSearchUrl,
  searchHref,
  searchKeyOf,
  type SearchUrl,
} from "./params";
import { ResultGrid } from "./ResultGrid";

const read = (query: string) => readSearchUrl(new URLSearchParams(query));
const D2 = "00000000-0000-0000-0000-0000000000d2";
const CENTRE = { lat: -26.32, lon: -48.82 };

function photo(
  id: string,
  capturedAt: string | null,
  source: ImageDetail["capture_source"] = capturedAt ? "exif_original" : "unknown",
): ImageDetail {
  return {
    id,
    filename: `DJI_${id}`,
    captured_at: capturedAt,
    capture_source: source,
    latitude: -26.32,
    longitude: -48.82,
    position_source: "exif_gps",
    device: { id: "d", label: "HD3", connected: false },
    relative_path: `obra/DJI_${id}.JPG`,
    absolute_path: null,
  };
}

function summary(fields: Partial<BrowseSummary>): BrowseSummary {
  return {
    total: 0,
    undated: 0,
    oldest: null,
    newest: null,
    excludedUnknownDate: null,
    excludedUnknownPosition: null,
    ...fields,
  };
}

/** The `data-id`s of the cards, and `|` where an item without one -- the
 * divider -- sits, in the HTML's order. Not a `<link>`: React 19 puts one
 * before the grid to preload each eager thumbnail. */
function layout(html: string): string[] {
  return [...html.matchAll(/<li(?=[\s>])([^>]*)>/g)].map(
    ([, attributes]) => /data-id="([^"]+)"/.exec(attributes ?? "")?.[1] ?? "|",
  );
}

describe("the URL of the listing (section 6.1)", () => {
  it("reads `todas` with its direction, and writes it back", () => {
    const url = read("todas&perto=-26.32,-48.82&raio=1000");

    expect(url.query).toEqual({ kind: "browse", order: "newest" });
    expect(read("todas&ordem=antigas&perto=1,2").query).toEqual({
      kind: "browse",
      order: "oldest",
    });
    expect(searchHref(url)).toBe("/busca?todas&perto=-26.32%2C-48.82&raio=1000");
    expect(searchHref(read("todas&ordem=antigas&perto=1,2"))).toBe(
      "/busca?todas&ordem=antigas&perto=1%2C2",
    );
  });

  it("comes after `q`, `parecidas` and `imagem`", () => {
    const circle = "&perto=1,2";

    expect(read(`q=obra&todas${circle}`).query).toEqual({ kind: "text", q: "obra" });
    expect(read(`parecidas=abc&todas${circle}`).query?.kind).toBe("similar");
    expect(read(`imagem&todas${circle}`).query?.kind).toBe("image");
  });

  it("is not a search without a circle: the home screen, with the filters", () => {
    const url = read(`todas&dispositivo=${D2}&de=2018-01-01`);

    expect(url.query).toBeNull();
    expect(searchHref(url)).toBe(`/?dispositivo=${D2}&de=2018-01-01`);
  });

  it("has a key per direction in the cache", () => {
    expect(searchKeyOf({ kind: "browse", order: "newest" }, null)).toBe(
      "browse:newest",
    );
    expect(searchKeyOf({ kind: "browse", order: "oldest" }, null)).toBe(
      "browse:oldest",
    );
  });
});

describe("what a page asks the API", () => {
  const filters = { ...NO_FILTERS, devices: [D2], near: CENTRE, radius: 5000 };

  it("is apiFilters() with the direction, and the cursor after the first page", () => {
    expect(apiBrowseQuery(filters, "newest", 40, null, 300)).toEqual({
      limit: 40,
      order: "newest",
      device_id: [D2],
      near_lat: -26.32,
      near_lon: -48.82,
      radius_m: 5000,
    });
    expect(apiBrowseQuery(filters, "oldest", 100, "abc", 300)).toMatchObject({
      limit: 100,
      order: "oldest",
      after: "abc",
    });
  });

  it("is serialized by openapi-fetch onto /images/browse", async () => {
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

    await client.GET("/api/v1/images/browse", {
      params: { query: apiBrowseQuery(filters, "oldest", 100, "abc", 300) },
    });

    const url = new URL(requested);
    expect(url.pathname).toBe("/api/v1/images/browse");
    expect(url.searchParams.get("order")).toBe("oldest");
    expect(url.searchParams.get("after")).toBe("abc");
    expect(url.searchParams.get("limit")).toBe("100");
  });
});

describe("the pages (section 6.3)", () => {
  const first = {
    results: [photo("a", "2026-10-02T17:45:00"), photo("b", "2026-01-02T09:00:00")],
    nextAfter: "b",
  };
  const second = {
    results: [photo("c", "2025-05-05T05:05:00"), photo("d", null)],
    nextAfter: null,
  };

  it("are 40, and then the max_search_limit", () => {
    expect(browsePageLimit(null, 100)).toBe(PAGE_SIZE);
    expect(PAGE_SIZE).toBe(40);
    expect(browsePageLimit("b", 100)).toBe(100);
  });

  it("append: the grid is the first page followed by the second", () => {
    const html = renderToStaticMarkup(
      <ResultGrid results={flattenPages([first, second])} corner={dateCorner} />,
    );

    expect(layout(html)).toEqual(["a", "b", "c", "d"]);
  });

  it("show 'Mostrar mais' while the last page has a next_after", () => {
    expect(canBrowseMore([first])).toBe(true);
    expect(canBrowseMore([first, second])).toBe(false);
    expect(canBrowseMore([])).toBe(false);
    expect(progressText(140, 6102)).toBe("140 de 6.102");
  });
});

describe("the corner and the divider", () => {
  it("writes the date from the text, at midnight and in a daylight-saving jump", () => {
    expect(cardDateText("2018-07-14T15:32:05")).toBe("14/07/2018 15:32");
    expect(cardDateText("2019-01-01T00:00:00")).toBe("01/01/2019 00:00");
    // 4 Nov 2018, 00:15 did not exist in São Paulo: the clocks jumped to 01:00.
    expect(cardDateText("2018-11-04T00:15:00")).toBe("04/11/2018 00:15");
  });

  it("says where the date came from", () => {
    expect(dateCorner(photo("a", "2018-07-14T15:32:05"))).toEqual({
      text: "14/07/2018 15:32",
      title: "Tirada em 14/07/2018 às 15:32, pelo relógio da câmera",
    });
    expect(dateCorner(photo("a", "2018-07-14T15:32:05", "exif_digitized"))?.title).toBe(
      "Digitalizada em 14/07/2018 às 15:32",
    );
  });

  it("leaves the corner empty for a photo with no date", () => {
    expect(dateCorner(photo("a", null))).toBeNull();

    const html = renderToStaticMarkup(
      <ResultGrid results={[photo("a", null)]} corner={dateCorner} />,
    );
    expect(html).not.toContain("title=");
    expect(html).toContain('aria-label="DJI_a.JPG, HD3 · desconectado."');
  });

  it("puts the date in the corner and in what a screen reader says", () => {
    const html = renderToStaticMarkup(
      <ResultGrid results={[photo("a", "2018-07-14T15:32:05")]} corner={dateCorner} />,
    );

    expect(html).toContain(">14/07/2018 15:32<");
    expect(html).toContain("Tirada em 14/07/2018 às 15:32, pelo relógio da câmera");
  });

  it("goes before the first undated photo, in both directions", () => {
    const newest = [
      photo("n", "2020-01-01T00:00:00"),
      photo("o", "2018-01-01T00:00:00"),
      photo("u1", null),
      photo("u2", null),
    ];
    const oldest = [newest[1]!, newest[0]!, newest[2]!, newest[3]!];

    for (const results of [newest, oldest]) {
      const beforeId = firstUndatedId(results);
      expect(beforeId).toBe("u1");
      const html = renderToStaticMarkup(
        <ResultGrid
          results={results}
          corner={dateCorner}
          divider={{
            beforeId: beforeId ?? "",
            title: dividerTitle(305),
            note: DIVIDER_NOTE,
          }}
        />,
      );
      expect(layout(html).slice(2)).toEqual(["|", "u1", "u2"]);
      expect(html).toContain("Sem data de captura · 305 fotos");
      expect(html).toContain(DIVIDER_NOTE);
    }
  });

  it("is not there without undated photos", () => {
    expect(firstUndatedId([photo("a", "2020-01-01T00:00:00")])).toBeNull();
    expect(dividerTitle(1)).toBe("Sem data de captura · 1 foto");
  });
});

describe("the header", () => {
  it("counts the photos of the place, their span and the undated", () => {
    expect(
      browseHeaderText(
        summary({
          total: 6102,
          undated: 305,
          oldest: "2015-01-03T09:00:00",
          newest: "2026-10-02T17:45:00",
        }),
      ),
    ).toBe("6.102 fotos daqui, de 03/01/2015 a 02/10/2026 · 305 sem data");
  });

  it("drops the span without a dated photo, and the last part without an undated one", () => {
    expect(browseHeaderText(summary({ total: 12, undated: 12 }))).toBe(
      "12 fotos daqui · 12 sem data",
    );
    expect(
      browseHeaderText(
        summary({
          total: 2,
          oldest: "2018-01-01T10:00:00",
          newest: "2019-01-01T10:00:00",
        }),
      ),
    ).toBe("2 fotos daqui, de 01/01/2018 a 01/01/2019");
  });

  it("is singular for one photo, and one day for one day", () => {
    expect(
      browseHeaderText(
        summary({
          total: 1,
          oldest: "2018-07-14T15:32:05",
          newest: "2018-07-14T15:32:05",
        }),
      ),
    ).toBe("1 foto daqui, em 14/07/2018");
    expect(browseHeaderText(summary({ total: 0 }))).toBe("Nenhuma foto daqui");
  });

  it("says the radius when the circle is empty", () => {
    expect(emptyBrowseText(1000, false)).toBe("Nenhuma foto até 1 km daqui.");
    expect(emptyBrowseText(300, true)).toBe(
      "Nenhuma foto até 300 m daqui com esses filtros.",
    );
  });
});

describe("the doors (section 6.2)", () => {
  const filters = {
    ...NO_FILTERS,
    devices: [D2],
    from: "2018-01-01",
    to: "2018-12-31",
  };

  it("from the home screen with a circle: the listing, with its filters", () => {
    const home: SearchUrl = {
      query: null,
      filters: { ...filters, near: CENTRE, radius: 1000 },
      photo: null,
    };

    expect(searchHref(browseSearch(home))).toBe(
      `/busca?todas&dispositivo=${D2}&de=2018-01-01&ate=2018-12-31` +
        "&perto=-26.32%2C-48.82&raio=1000",
    );
  });

  it("from a search with a circle: the same circle, without the text", () => {
    const search: SearchUrl = {
      query: { kind: "text", q: "construção" },
      filters: { ...filters, near: CENTRE, radius: 5000, nearOf: "img" },
      photo: "img",
    };

    expect(searchHref(browseSearch(search))).toBe(
      `/busca?todas&dispositivo=${D2}&de=2018-01-01&ate=2018-12-31` +
        "&perto=-26.32%2C-48.82&raio=5000&perto_de=img",
    );
  });

  it("from the balloon: the map's filters, the cell's radius, not its search", () => {
    const map: SearchUrl = {
      query: { kind: "text", q: "telhado azul" },
      filters,
      photo: null,
    };

    expect(searchHref(nearPointBrowse(map, CENTRE, 10_000))).toBe(
      `/busca?todas&dispositivo=${D2}&de=2018-01-01&ate=2018-12-31` +
        "&perto=-26.32%2C-48.82&raio=10000",
    );
  });

  it("all say the same thing", () => {
    expect(BROWSE_ACTION_TEXT).toBe("Ver todas as fotos daqui, por data");
  });
});

describe("the messages (section 6.4)", () => {
  it("400 is the search's, 404 is a list that changed, 0 is no answer", () => {
    expect(errorMessage("browse", 400)).toBe(BAD_FILTERS);
    expect(errorMessage("browse", 404)).toBe(LIST_CHANGED);
    expect(LIST_CHANGED).toBe("A lista mudou desde a última página.");
    expect(errorMessage("browse", 0)).toBe(NO_ANSWER);
  });
});
