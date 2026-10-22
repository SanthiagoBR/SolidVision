import { describe, expect, it } from "vitest";

import { BROWSER_LINKS, opensInBrowser } from "./links";

describe("the links that open in the default browser (RFC-040 section 5.6)", () => {
  it("are the OpenStreetMap's copyright page, and only it", () => {
    expect(BROWSER_LINKS).toEqual(["https://www.openstreetmap.org/copyright"]);
    expect(opensInBrowser("https://www.openstreetmap.org/copyright")).toBe(true);
  });

  it("are not the same host with another path, a query or a fragment", () => {
    for (const url of [
      "https://www.openstreetmap.org/",
      "https://www.openstreetmap.org/copyright/",
      "https://www.openstreetmap.org/copyright/pt",
      "https://www.openstreetmap.org/copyright?x=1",
      "https://www.openstreetmap.org/copyright#licença",
      "https://www.openstreetmap.org/#map=12/-26.3/-48.8",
    ]) {
      expect(opensInBrowser(url), url).toBe(false);
    }
  });

  it("are not http:, nor a host that looks like it", () => {
    for (const url of [
      "http://www.openstreetmap.org/copyright",
      "https://openstreetmap.org/copyright",
      "https://www.openstreetmap.org.example.com/copyright",
      "https://www.openstreetmap.org@example.com/copyright",
      "https://user@www.openstreetmap.org/copyright",
      "https://www.openstreetmap.org:8443/copyright",
      "https://tile.openstreetmap.org/copyright",
      "file:///C:/copyright",
      "javascript:alert(1)",
      "not a url",
      "",
    ]) {
      expect(opensInBrowser(url), url).toBe(false);
    }
  });
});
