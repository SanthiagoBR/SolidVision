/**
 * The OpenStreetMap layer, and when it steps aside (RFC-040 sections 2.6 and
 * 5.2).
 *
 * **The URL is the one the tile usage policy names**, from zoom 3 to 18, over
 * the outline of the states. A tile that fails leaves the outline visible
 * beneath it: that is how "no network" becomes "the outline", without
 * detecting anything. Nothing here keeps tiles for later -- *"Offline use is
 * not permitted on tile.openstreetmap.org"* -- and the only cache is
 * Chromium's HTTP cache, which follows the headers, as the policy asks.
 *
 * **The pause.** When the layer finishes a screen (the Leaflet `load`) with no
 * `tileload` and some `tileerror`, it leaves the map, and a quiet strip says
 * so, with "Tentar de novo": without a network a drag does not become a line
 * per tile in `shell.log`, each with the place in its path (section 2.7). The
 * layer comes back on the window's `online`, on the button, and on the next
 * opening of the screen. A tile from the HTTP cache is a `tileload`, and keeps
 * the layer.
 *
 * **The choice of "Fundo do OpenStreetMap"** stays in the window's
 * `localStorage`, read in a `try`; without it, on, as D4 asks.
 */

export const OSM_TILES_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
export const OSM_COPYRIGHT_URL = "https://www.openstreetmap.org/copyright";
/** In a new window, which the shell opens in the default browser and nowhere
 * else (`electron/links.ts`); in a browser, a new tab. */
export const OSM_ATTRIBUTION =
  `© <a href="${OSM_COPYRIGHT_URL}" target="_blank" rel="noreferrer">` +
  "colaboradores do OpenStreetMap</a>";
export const OSM_MIN_ZOOM = 3;
export const OSM_MAX_ZOOM = 18;

export const OFFLINE_TEXT =
  "Sem conexão com o OpenStreetMap: o fundo é o contorno dos estados.";

/** One screen of tiles: from the layer's `loading` to its `load`. */
export interface TileScreen {
  readonly loaded: number;
  readonly failed: number;
}

export const NEW_SCREEN: TileScreen = { loaded: 0, failed: 0 };

export type TileEvent = "loading" | "tileload" | "tileerror";

export function countTile(screen: TileScreen, event: TileEvent): TileScreen {
  switch (event) {
    case "loading":
      return NEW_SCREEN;
    case "tileload":
      return { ...screen, loaded: screen.loaded + 1 };
    case "tileerror":
      return { ...screen, failed: screen.failed + 1 };
  }
}

/** At the layer's `load`: a screen where no tile loaded and some failed. */
export function pausesAt(screen: TileScreen): boolean {
  return screen.loaded === 0 && screen.failed > 0;
}

export const OSM_STORAGE_KEY = "solidvision.mapa.fundo-osm";

type Store = Pick<Storage, "getItem" | "setItem">;

/** The window's storage, or `null` where reading it throws. */
export function windowStorage(): Store | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

/** On, unless the person turned it off and the storage remembers. */
export function readOsmChoice(storage: Store | null): boolean {
  try {
    return storage?.getItem(OSM_STORAGE_KEY) !== "off";
  } catch {
    return true;
  }
}

export function writeOsmChoice(storage: Store | null, on: boolean): void {
  try {
    storage?.setItem(OSM_STORAGE_KEY, on ? "on" : "off");
  } catch {
    // A storage that refuses: the choice lasts until the screen closes.
  }
}
