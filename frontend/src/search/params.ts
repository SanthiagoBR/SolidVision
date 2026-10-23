/**
 * The search lives in the URL (RFC-038 section 5.1).
 *
 *     URL                      what
 *     /busca?q=…               the search by text
 *     /busca?parecidas=<id>    "Parecidas com esta"
 *     /busca?imagem            the dropped photo; its results stay in the page
 *     /busca?todas&perto=…     every photo of the circle, by date (RFC-044);
 *                              `&ordem=antigas` for the oldest first
 *     …&foto=<id>              the detail, open over the grid
 *
 * and the filters beside them, with the interface's names: `dispositivo`
 * (repeated), `de` and `ate` (`AAAA-MM-DD`, both inclusive), `perto`
 * (`lat,lon`), `raio` (metres) and `perto_de` (the photo that gave the
 * centre, for the filter to say its name).
 *
 * In the URL so that back and forward work, the search survives the
 * "Reconectando…" that reloads the page, and the same search is the same key
 * in TanStack Query's cache. One function, `apiQuery()`, turns the
 * interface's names into the API's.
 */

import { isInverted, isIsoDate, nextDay } from "./dates";
import { effectiveRadius } from "./radius";

/** The direction of the capture date in a listing (RFC-044 section 3.1). */
export type BrowseOrder = "newest" | "oldest";

export type SearchQuery =
  | { readonly kind: "text"; readonly q: string }
  | { readonly kind: "similar"; readonly imageId: string }
  | { readonly kind: "image" }
  | { readonly kind: "browse"; readonly order: BrowseOrder };

export type SearchKind = SearchQuery["kind"];

export interface Point {
  readonly lat: number;
  readonly lon: number;
}

export interface Filters {
  /** Device ids; none means every device. */
  readonly devices: readonly string[];
  /** The first day, inclusive. */
  readonly from: string | null;
  /** The last day, inclusive: the API is sent the day after. */
  readonly to: string | null;
  readonly near: Point | null;
  /** The radius chosen, in metres; `null` until somebody chooses one. */
  readonly radius: number | null;
  /** The photo whose position is `near`. */
  readonly nearOf: string | null;
}

export interface SearchUrl {
  readonly query: SearchQuery | null;
  readonly filters: Filters;
  /** The photo whose detail is open. */
  readonly photo: string | null;
}

export const NO_FILTERS: Filters = {
  devices: [],
  from: null,
  to: null,
  near: null,
  radius: null,
  nearOf: null,
};

export function hasFilters(filters: Filters): boolean {
  return (
    filters.devices.length > 0 ||
    filters.from !== null ||
    filters.to !== null ||
    filters.near !== null
  );
}

// ---------------------------------------------------------------------------
// The URL

export function readSearchUrl(params: URLSearchParams): SearchUrl {
  const filters = readFilters(params);
  const query = readQuery(params);
  return {
    // Without a circle, `todas` is not a search: the interface lists a place,
    // and the URL opens the home screen with the other filters (RFC-044 6.1).
    query: query?.kind === "browse" && filters.near === null ? null : query,
    filters,
    photo: params.get("foto") || null,
  };
}

/** The listing's own parameters, which the home screen does not keep. */
export const BROWSE_PARAMS = ["todas", "ordem"] as const;

function readQuery(params: URLSearchParams): SearchQuery | null {
  const q = params.get("q");
  if (q !== null && q.trim() !== "") {
    return { kind: "text", q };
  }
  const similar = params.get("parecidas");
  if (similar) {
    return { kind: "similar", imageId: similar };
  }
  if (params.has("imagem")) {
    return { kind: "image" };
  }
  if (params.has("todas")) {
    return {
      kind: "browse",
      order: params.get("ordem") === "antigas" ? "oldest" : "newest",
    };
  }
  return null;
}

export function readFilters(params: URLSearchParams): Filters {
  const from = params.get("de");
  const to = params.get("ate");
  const near = readPoint(params.get("perto"));
  const radius = Number(params.get("raio") ?? "");
  return {
    devices: [...new Set(params.getAll("dispositivo").filter(Boolean))],
    from: from !== null && isIsoDate(from) ? from : null,
    to: to !== null && isIsoDate(to) ? to : null,
    near,
    radius:
      near !== null && params.has("raio") && Number.isFinite(radius) && radius > 0
        ? radius
        : null,
    nearOf: near !== null ? params.get("perto_de") || null : null,
  };
}

/** What a typed coordinate's own field says when `readPoint()` refuses it:
 * the one message, wherever a coordinate is typed by hand. */
export const COORDINATES_INVALID_TEXT =
  "Latitude e longitude separadas por vírgula, de -90 a 90 e de -180 a 180.";

/** `lat,lon`, trimmed around each number; `null` for anything else, so a
 * typed coordinate and the URL's own `perto` share the one rule. */
export function readPoint(text: string | null): Point | null {
  if (text === null) {
    return null;
  }
  const [lat, lon, ...rest] = text.split(",").map((part) => Number(part.trim()));
  if (
    rest.length > 0 ||
    lat === undefined ||
    lon === undefined ||
    !Number.isFinite(lat) ||
    !Number.isFinite(lon) ||
    Math.abs(lat) > 90 ||
    Math.abs(lon) > 180
  ) {
    return null;
  }
  return { lat, lon };
}

/** The query string of a search screen, in a stable order; `imagem` without
 * a value. */
export function searchQueryString(url: SearchUrl): string {
  const parts: string[] = [];
  const add = (name: string, value: string) =>
    parts.push(`${name}=${encodeURIComponent(value)}`);
  const query = url.query;
  if (query?.kind === "text") {
    add("q", query.q);
  } else if (query?.kind === "similar") {
    add("parecidas", query.imageId);
  } else if (query?.kind === "image") {
    parts.push("imagem");
  } else if (query?.kind === "browse") {
    parts.push("todas");
    if (query.order === "oldest") {
      add("ordem", "antigas");
    }
  }
  const filters = url.filters;
  for (const device of filters.devices) {
    add("dispositivo", device);
  }
  if (filters.from !== null) {
    add("de", filters.from);
  }
  if (filters.to !== null) {
    add("ate", filters.to);
  }
  if (filters.near !== null) {
    add("perto", `${filters.near.lat},${filters.near.lon}`);
    if (filters.radius !== null) {
      add("raio", String(filters.radius));
    }
    if (filters.nearOf !== null) {
      add("perto_de", filters.nearOf);
    }
  }
  if (url.photo !== null) {
    add("foto", url.photo);
  }
  return parts.join("&");
}

/** Where a search, or the home screen with its filters, lives. */
export function searchHref(url: SearchUrl): string {
  const path = url.query === null ? "/" : "/busca";
  const query = searchQueryString(url);
  return query === "" ? path : `${path}?${query}`;
}

/** The part of a search's cache key that names its query: the filters and
 * the page are the rest. A dropped photo is its token: a new photo is a new
 * search, whatever its name. */
export function searchKeyOf(query: SearchQuery, photoToken: number | null): string {
  switch (query.kind) {
    case "text":
      return `text:${query.q}`;
    case "similar":
      return `similar:${query.imageId}`;
    case "image":
      return `image:${photoToken ?? 0}`;
    case "browse":
      return `browse:${query.order}`;
  }
}

// ---------------------------------------------------------------------------
// The API

export interface ApiFilters {
  device_id?: string[];
  captured_from?: string;
  captured_to?: string;
  near_lat?: number;
  near_lon?: number;
  radius_m?: number;
}

export interface ApiSearchQuery extends ApiFilters {
  limit: number;
}

/**
 * The filters with the API's names, and nothing a filter did not set.
 *
 * The dates go as `AAAA-MM-DD`, which the API reads as midnight, camera-local;
 * the end as the day after, because the API's end is exclusive. An "até"
 * before the "de" is not sent. No `Date`, no `Z`, no offset anywhere.
 *
 * Without the `limit` since RFC-040: the map sends the same filters, and has
 * no page (section 5.5).
 */
export function apiFilters(filters: Filters, minRadiusM: number): ApiFilters {
  const query: ApiFilters = {};
  if (filters.devices.length > 0) {
    query.device_id = [...filters.devices];
  }
  if (filters.from !== null) {
    query.captured_from = filters.from;
  }
  if (filters.to !== null && !isInverted(filters.from, filters.to)) {
    query.captured_to = nextDay(filters.to);
  }
  if (filters.near !== null) {
    query.near_lat = filters.near.lat;
    query.near_lon = filters.near.lon;
    query.radius_m = effectiveRadius(filters.radius, minRadiusM);
  }
  return query;
}

/** `apiFilters()` with the page size of a search. */
export function apiQuery(
  filters: Filters,
  limit: number,
  minRadiusM: number,
): ApiSearchQuery {
  return { limit, ...apiFilters(filters, minRadiusM) };
}

export interface ApiBrowseQuery extends ApiSearchQuery {
  order: BrowseOrder;
  after?: string;
}

/** A page of the listing (RFC-044 section 6.1): `apiFilters()` as for every
 * search, with the direction and, after the first page, the cursor beside
 * them. */
export function apiBrowseQuery(
  filters: Filters,
  order: BrowseOrder,
  limit: number,
  after: string | null,
  minRadiusM: number,
): ApiBrowseQuery {
  const query: ApiBrowseQuery = { ...apiQuery(filters, limit, minRadiusM), order };
  if (after !== null) {
    query.after = after;
  }
  return query;
}

// ---------------------------------------------------------------------------
// The devices

/** Check or uncheck one device. Every one checked is the same as none: all. */
export function toggleDevice(
  selected: readonly string[],
  deviceId: string,
  allDeviceIds: readonly string[],
): string[] {
  const next = selected.includes(deviceId)
    ? selected.filter((id) => id !== deviceId)
    : [...selected, deviceId];
  const covers =
    allDeviceIds.length > 0 && allDeviceIds.every((id) => next.includes(id));
  return covers ? [] : next;
}

/** The pill: `Dispositivos · todos (4)`, `Dispositivos · HD3`, or
 * `Dispositivos · 2 de 4`. */
export function devicePillText(
  devices: readonly { readonly id: string; readonly label: string }[] | null,
  selected: readonly string[],
): string {
  if (devices === null) {
    return "Dispositivos · todos";
  }
  const chosen = devices.filter((device) => selected.includes(device.id));
  if (chosen.length === 0 || chosen.length === devices.length) {
    return `Dispositivos · todos (${devices.length})`;
  }
  if (chosen.length === 1 && chosen[0] !== undefined) {
    return `Dispositivos · ${chosen[0].label}`;
  }
  return `Dispositivos · ${chosen.length} de ${devices.length}`;
}
