import { describe, expect, it } from "vitest";

import {
  countTile,
  NEW_SCREEN,
  OSM_ATTRIBUTION,
  OSM_COPYRIGHT_URL,
  OSM_STORAGE_KEY,
  OSM_TILES_URL,
  pausesAt,
  readOsmChoice,
  type TileEvent,
  type TileScreen,
  writeOsmChoice,
} from "./tiles";

const screenOf = (events: TileEvent[], from: TileScreen = NEW_SCREEN) =>
  events.reduce(countTile, from);

describe("the pause of the tiles (RFC-040 section 5.2)", () => {
  it("pauses a screen with only failures", () => {
    expect(pausesAt(screenOf(["loading", "tileerror", "tileerror", "tileerror"]))).toBe(
      true,
    );
  });

  it("does not pause a screen where one tile loaded", () => {
    expect(pausesAt(screenOf(["loading", "tileerror", "tileload", "tileerror"]))).toBe(
      false,
    );
  });

  it("does not pause a screen with no failure", () => {
    expect(pausesAt(screenOf(["loading", "tileload"]))).toBe(false);
    expect(pausesAt(NEW_SCREEN)).toBe(false);
  });

  it("counts each screen from its own loading", () => {
    const loaded = screenOf(["loading", "tileload", "tileload"]);

    expect(pausesAt(screenOf(["loading", "tileerror"], loaded))).toBe(true);
  });
});

describe("the layer", () => {
  it("is the URL of the tile usage policy, with the attribution's link", () => {
    expect(OSM_TILES_URL).toBe("https://tile.openstreetmap.org/{z}/{x}/{y}.png");
    expect(OSM_ATTRIBUTION).toContain(`href="${OSM_COPYRIGHT_URL}"`);
    expect(OSM_ATTRIBUTION).toContain("colaboradores do OpenStreetMap");
    expect(OSM_COPYRIGHT_URL).toBe("https://www.openstreetmap.org/copyright");
  });
});

describe("the choice of 'Fundo do OpenStreetMap'", () => {
  const memory = () => {
    const values = new Map<string, string>();
    return {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => void values.set(key, value),
    };
  };

  it("is on without a storage, and with one that remembers nothing", () => {
    expect(readOsmChoice(null)).toBe(true);
    expect(readOsmChoice(memory())).toBe(true);
  });

  it("is what the person chose, when the storage remembers", () => {
    const storage = memory();
    writeOsmChoice(storage, false);

    expect(storage.getItem(OSM_STORAGE_KEY)).toBe("off");
    expect(readOsmChoice(storage)).toBe(false);
    writeOsmChoice(storage, true);
    expect(readOsmChoice(storage)).toBe(true);
  });

  it("is on when the storage throws, and a write that throws is no error", () => {
    const throwing = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
    };

    expect(readOsmChoice(throwing)).toBe(true);
    expect(() => writeOsmChoice(throwing, false)).not.toThrow();
  });
});
