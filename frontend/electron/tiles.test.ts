import { describe, expect, it } from "vitest";

import {
  isTileRequest,
  PROJECT_URL,
  TILE_URL_PATTERNS,
  tileUserAgent,
  withTileUserAgent,
} from "./tiles";

const CHROMIUM =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) " +
  "SolidVision/0.1.0 Chrome/140.0.0.0 Electron/44.9.0 Safari/537.36";

describe("the User-Agent of the tiles (RFC-040 section 5.6)", () => {
  it("names the app and its version, and imitates no browser", () => {
    expect(tileUserAgent("0.1.0")).toBe(
      "SolidVision/0.1.0 (+https://github.com/SanthiagoBR/SolidVision)",
    );
    expect(tileUserAgent("0.1.0")).not.toMatch(/Mozilla|Chrome|Safari|Electron/);
    expect(PROJECT_URL).toBe("https://github.com/SanthiagoBR/SolidVision");
  });

  it("goes to the tile host, in place of Chromium's", () => {
    const headers = withTileUserAgent(
      "https://tile.openstreetmap.org/12/1492/2358.png",
      { "User-Agent": CHROMIUM, Accept: "image/avif,image/webp,*/*" },
      "0.1.0",
    );

    expect(headers).toEqual({
      Accept: "image/avif,image/webp,*/*",
      "User-Agent": "SolidVision/0.1.0 (+https://github.com/SanthiagoBR/SolidVision)",
    });
  });

  it("goes nowhere else: the API, another host, http:", () => {
    const headers = { "User-Agent": CHROMIUM };
    for (const url of [
      "http://127.0.0.1:52011/api/v1/images/map?min_lat=-27",
      "https://www.openstreetmap.org/copyright",
      "http://tile.openstreetmap.org/1/0/0.png",
      "https://tile.openstreetmap.org.example.com/1/0/0.png",
    ]) {
      expect(withTileUserAgent(url, headers, "0.1.0"), url).toBe(headers);
      expect(isTileRequest(url), url).toBe(false);
    }
  });

  it("is asked for by the filter of the tile host only", () => {
    expect(TILE_URL_PATTERNS).toEqual(["https://tile.openstreetmap.org/*"]);
  });

  it("replaces a User-Agent of any case", () => {
    const headers = withTileUserAgent(
      "https://tile.openstreetmap.org/3/2/4.png",
      { "user-agent": CHROMIUM },
      "1.2.3",
    );

    expect(headers).toEqual({
      "User-Agent": "SolidVision/1.2.3 (+https://github.com/SanthiagoBR/SolidVision)",
    });
  });
});
