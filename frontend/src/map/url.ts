/**
 * The map lives in the URL, as the search does (RFC-040 section 5.1).
 *
 *     URL                                         what
 *     /mapa                                       the map, with all of Brazil
 *     /mapa?centro=-26.3214,-48.8163&zoom=9       a framing, written at the end
 *                                                 of every drag or zoom, with
 *                                                 no entry in the history
 *     …&q=telhado                                 the search the balloon of a
 *                                                 cell runs: `q`, `parecidas`
 *                                                 or `imagem`, the search's names
 *     …&dispositivo=…&de=…&ate=…                  the filters, by the search's
 *                                                 names
 *
 * `perto`, `raio`, `perto_de` and `foto` do not enter: the map's area is the
 * visible one -- and neither does `todas`, the listing by date (RFC-044), which
 * is a circle's question: the balloon of a cell has its own button for it. Back, forward and the "Reconectando…" that reloads the page
 * return to the same framing; the whole query string leaves `shell.log` by
 * `redactUrl()`, the framing with it (RFC-038 section 5.6).
 *
 * No Leaflet here: the search screen links to the map, and this module is in
 * the part of the build every screen loads.
 */

import {
  type Filters,
  type Point,
  readSearchUrl,
  searchQueryString,
  type SearchQuery,
} from "../search/params";
import { MAX_ZOOM, MIN_ZOOM } from "./precision";

export const MAP_PATH = "/mapa";

export interface MapView {
  readonly center: Point;
  readonly zoom: number;
}

export interface MapUrl {
  /** The search a cell's balloon runs; `null` opens the home screen. */
  readonly query: SearchQuery | null;
  /** The devices and the dates; never a circle. */
  readonly filters: Filters;
  /** `null`: all of Brazil. */
  readonly view: MapView | null;
}

/** The filters without the circle, which the map never carries. */
export function withoutCircle(filters: Filters): Filters {
  return { ...filters, near: null, radius: null, nearOf: null };
}

/** The search a cell's balloon may run: never the listing by date. */
function carried(query: SearchQuery | null): SearchQuery | null {
  return query?.kind === "browse" ? null : query;
}

export function readMapUrl(params: URLSearchParams): MapUrl {
  const search = readSearchUrl(params);
  return {
    query: carried(search.query),
    filters: withoutCircle(search.filters),
    view: readView(params),
  };
}

/** Both halves of the framing, or none: a centre off the planet, or a zoom
 * that is not a whole number from 3 to 18, is ignored. */
function readView(params: URLSearchParams): MapView | null {
  const centre = params.get("centro");
  const zoomText = params.get("zoom");
  if (centre === null || zoomText === null || zoomText.trim() === "") {
    return null;
  }
  const parts = centre.split(",").map((part) => part.trim());
  const zoom = Number(zoomText);
  if (parts.length !== 2 || parts.some((part) => part === "")) {
    return null;
  }
  const [lat, lon] = parts.map(Number);
  if (
    lat === undefined ||
    lon === undefined ||
    !Number.isFinite(lat) ||
    !Number.isFinite(lon) ||
    Math.abs(lat) > 90 ||
    Math.abs(lon) > 180 ||
    !Number.isInteger(zoom) ||
    zoom < MIN_ZOOM ||
    zoom > MAX_ZOOM
  ) {
    return null;
  }
  return { center: { lat, lon }, zoom };
}

const round4 = (value: number) => Math.round(value * 10_000) / 10_000;

/** A framing as the URL keeps it: the centre with four places (~11 m). */
export function roundView(center: Point, zoom: number): MapView {
  return {
    center: { lat: round4(center.lat), lon: round4(center.lon) },
    zoom: Math.round(zoom),
  };
}

/** The same framing is the same text. */
export function viewKey(view: MapView | null): string {
  return view === null ? "" : `${view.center.lat},${view.center.lon},${view.zoom}`;
}

/** The query string of the map: the framing first, then the search's own. */
export function mapQueryString(url: MapUrl): string {
  const parts: string[] = [];
  if (url.view !== null) {
    parts.push(
      `centro=${url.view.center.lat},${url.view.center.lon}`,
      `zoom=${url.view.zoom}`,
    );
  }
  const search = searchQueryString({
    query: carried(url.query),
    filters: withoutCircle(url.filters),
    photo: null,
  });
  if (search !== "") {
    parts.push(search);
  }
  return parts.join("&");
}

export function mapHref(url: MapUrl): string {
  const query = mapQueryString(url);
  return query === "" ? MAP_PATH : `${MAP_PATH}?${query}`;
}

/** The map with a search and its filters: "ou escolha um lugar no mapa". */
export function mapHrefFor(query: SearchQuery | null, filters: Filters): string {
  return mapHref({ query, filters, view: null });
}
