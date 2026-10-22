/**
 * The area the page asks for, and when it asks again (RFC-040 sections 2.5
 * and 5.3).
 *
 * **The box is the visible area with a quarter of its width and of its height
 * as a margin on each side**, cut to the API's bands -- latitude in ±90,
 * longitude in ±180 -- and never inverted. The route filters the photos by the
 * box before it groups them, so a cell the edge crosses counts only its inside;
 * the margin puts that edge off the screen. The quarter is a choice.
 *
 * **A new request only when it is needed:**
 *
 *     when                                     the page
 *     the visible area leaves the last box     asks again
 *     the precision of the zoom changes        asks again
 *     a filter changes                         asks again
 *     otherwise -- a short drag                asks nothing
 *
 * The edges are rounded outwards to four places (~11 m): the box still holds
 * the visible area, and the request carries no more digits than that.
 */

import { type ApiFilters, apiFilters, type Filters } from "../search/params";
import { precisionForZoom } from "./precision";

/** The filters of the map, with the API's names: the search's, by the
 * search's translation, without the circle -- the map's area is the visible
 * one (RFC-032 section 7) -- and without the page (section 5.5). */
export type MapFilters = Pick<
  ApiFilters,
  "device_id" | "captured_from" | "captured_to"
>;

export function mapFilters(filters: Filters): MapFilters {
  const { device_id, captured_from, captured_to } = apiFilters(
    { ...filters, near: null, radius: null, nearOf: null },
    0,
  );
  return {
    ...(device_id !== undefined && { device_id }),
    ...(captured_from !== undefined && { captured_from }),
    ...(captured_to !== undefined && { captured_to }),
  };
}

export interface Box {
  readonly south: number;
  readonly west: number;
  readonly north: number;
  readonly east: number;
}

export const MARGIN = 0.25;
const PLACES = 10_000;

const clamp = (value: number, limit: number) =>
  Math.min(limit, Math.max(-limit, value));

/** The box inside the API's bands, the right way round. */
export function clampBox(box: Box): Box {
  const south = clamp(box.south, 90);
  const north = clamp(box.north, 90);
  const west = clamp(box.west, 180);
  const east = clamp(box.east, 180);
  return {
    south: Math.min(south, north),
    north: Math.max(south, north),
    west: Math.min(west, east),
    east: Math.max(west, east),
  };
}

/** The visible area and a quarter of it on each side, cut to the bands and
 * rounded outwards. */
export function requestBox(visible: Box): Box {
  const area = clampBox(visible);
  const height = area.north - area.south;
  const width = area.east - area.west;
  return clampBox({
    south: Math.floor((area.south - height * MARGIN) * PLACES) / PLACES,
    west: Math.floor((area.west - width * MARGIN) * PLACES) / PLACES,
    north: Math.ceil((area.north + height * MARGIN) * PLACES) / PLACES,
    east: Math.ceil((area.east + width * MARGIN) * PLACES) / PLACES,
  });
}

export function containsBox(outer: Box, inner: Box): boolean {
  return (
    inner.south >= outer.south &&
    inner.north <= outer.north &&
    inner.west >= outer.west &&
    inner.east <= outer.east
  );
}

export function intersects(first: Box, second: Box): boolean {
  return (
    first.south <= second.north &&
    first.north >= second.south &&
    first.west <= second.east &&
    first.east >= second.west
  );
}

/** One request of the map: the box, the precision, and the filters with the
 * API's names. */
export interface MapRequest {
  readonly box: Box;
  readonly precision: number;
  readonly filters: MapFilters;
}

/** The same filters: `mapFilters()` writes its keys in one order. */
export function sameFilters(first: MapFilters, second: MapFilters): boolean {
  return JSON.stringify(first) === JSON.stringify(second);
}

/**
 * The request for this view: `last` itself when the visible area is inside its
 * box, at the same precision and with the same filters; a new one otherwise.
 */
export function planRequest(
  last: MapRequest | null,
  visible: Box,
  zoom: number,
  filters: MapFilters,
): MapRequest {
  const precision = precisionForZoom(zoom);
  if (
    last !== null &&
    last.precision === precision &&
    sameFilters(last.filters, filters) &&
    containsBox(last.box, clampBox(visible))
  ) {
    return last;
  }
  return { box: requestBox(visible), precision, filters };
}

/** The query string of `GET /images/map`. */
export interface ApiMapQuery extends MapFilters {
  min_lat: number;
  min_lon: number;
  max_lat: number;
  max_lon: number;
  precision: number;
}

export function apiMapQuery(request: MapRequest): ApiMapQuery {
  return {
    min_lat: request.box.south,
    min_lon: request.box.west,
    max_lat: request.box.north,
    max_lon: request.box.east,
    precision: request.precision,
    ...request.filters,
  };
}
