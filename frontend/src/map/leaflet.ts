/**
 * The Leaflet of the map screen (RFC-040 sections 5.2 and 5.3), used directly,
 * without `react-leaflet`: one screen, and the interface has no component
 * library (RFC-037 section 4.1).
 *
 * Three layers, from the bottom up:
 *
 *     layer              what
 *     the outline        the 27 federative units, always drawn: the land in a
 *                        light tone and the borders in a grey, in a pane of its
 *                        own beneath the tiles
 *     the OpenStreetMap  from zoom 3 to 18, a choice, over the outline. A tile
 *                        that fails leaves the outline visible beneath it
 *     the cells          over everything: rectangles on a canvas, with the
 *                        click area 8 px around each (the canvas `tolerance`)
 *
 * The map never leaves the world (`maxBounds`), so the visible area never
 * crosses ±180°, which the API refuses. The colours are the tokens of
 * `tokens.css`, read when the map is made: a canvas cannot read a CSS
 * variable.
 */

import type { FeatureCollection } from "geojson";
import * as L from "leaflet";

import type { Box } from "./box";
import outline from "./brazil-states.json";
import { type Cell, cellBox, countClass } from "./cells";
import { MAX_ZOOM, MIN_ZOOM } from "./precision";
import { OSM_ATTRIBUTION, OSM_MAX_ZOOM, OSM_MIN_ZOOM, OSM_TILES_URL } from "./tiles";
import type { MapView } from "./url";

export interface MapColors {
  readonly classes: readonly string[];
  readonly land: string;
  readonly border: string;
}

export function readColors(): MapColors {
  const style = getComputedStyle(document.documentElement);
  const token = (name: string) => style.getPropertyValue(name).trim();
  return {
    classes: [1, 2, 3, 4, 5].map((index) => token(`--color-map-${index}`)),
    land: token("--color-map-land"),
    border: token("--color-map-border"),
  };
}

/** The cells' fill, and the 1 px outline in the darkest tone (section 5.3).
 * The opacity and the outlines are choices. */
export const CELL_FILL_OPACITY = 0.75;
export const CELL_WEIGHT = 1;
export const CELL_HOVER_WEIGHT = 2;
export const CLICK_TOLERANCE_PX = 8;

export function boxOf(bounds: L.LatLngBounds): Box {
  return {
    south: bounds.getSouth(),
    west: bounds.getWest(),
    north: bounds.getNorth(),
    east: bounds.getEast(),
  };
}

function boundsOf(box: Box): L.LatLngBounds {
  return L.latLngBounds([box.south, box.west], [box.north, box.east]);
}

export interface LeafletMap {
  readonly map: L.Map;
  readonly tiles: L.TileLayer;
  readonly cells: L.FeatureGroup;
  /** The cell of each rectangle on the map, and the precision it was cut at. */
  readonly cellOf: WeakMap<
    L.Layer,
    { readonly cell: Cell; readonly precision: number }
  >;
  /** The limits of the outline: all of Brazil. */
  readonly brazil: L.LatLngBounds;
  readonly colors: MapColors;
}

export const FIT_PADDING: L.PointExpression = [16, 16];

/** The map, the three layers, and the first framing: the URL's, or Brazil. */
export function createMap(element: HTMLElement, view: MapView | null): LeafletMap {
  const colors = readColors();
  const map = L.map(element, {
    minZoom: MIN_ZOOM,
    maxZoom: MAX_ZOOM,
    maxBounds: L.latLngBounds([-90, -180], [90, 180]),
    maxBoundsViscosity: 1,
    worldCopyJump: false,
    zoomControl: false,
    attributionControl: false,
  });
  L.control
    .zoom({ position: "topleft", zoomInTitle: "Aproximar", zoomOutTitle: "Afastar" })
    .addTo(map);
  // The attribution in view over the map, and Leaflet's own prefix out: its
  // licence asks for the notice in the distribution, not on the map.
  L.control.attribution({ position: "bottomright", prefix: false }).addTo(map);

  const pane = map.createPane("outline");
  pane.style.zIndex = "150";
  const states = L.geoJSON(
    outline as unknown as FeatureCollection,
    {
      pane: "outline",
      interactive: false,
      style: {
        color: colors.border,
        weight: 1,
        opacity: 1,
        fillColor: colors.land,
        fillOpacity: 1,
      },
      renderer: L.canvas({ pane: "outline" }),
    } as L.GeoJSONOptions,
  ).addTo(map);
  const brazil = states.getBounds();

  const tiles = L.tileLayer(OSM_TILES_URL, {
    minZoom: OSM_MIN_ZOOM,
    maxZoom: OSM_MAX_ZOOM,
    noWrap: true,
    attribution: OSM_ATTRIBUTION,
  });

  const cells = L.featureGroup().addTo(map);

  if (view === null) {
    map.fitBounds(brazil, { padding: FIT_PADDING });
  } else {
    map.setView([view.center.lat, view.center.lon], view.zoom);
  }
  return { map, tiles, cells, cellOf: new WeakMap(), brazil, colors };
}

const renderers = new WeakMap<L.Map, L.Canvas>();

/** The canvas of the cells, one per map, with the click tolerance. */
function cellRenderer(map: L.Map): L.Canvas {
  let renderer = renderers.get(map);
  if (renderer === undefined) {
    renderer = L.canvas({ tolerance: CLICK_TOLERANCE_PX });
    renderers.set(map, renderer);
  }
  return renderer;
}

export function cellStyle(colors: MapColors, count: number): L.PathOptions {
  return {
    color: colors.classes[colors.classes.length - 1],
    weight: CELL_WEIGHT,
    opacity: 1,
    fillColor: colors.classes[countClass(count)],
    fillOpacity: CELL_FILL_OPACITY,
  };
}

/** The cells of one response, in place of the ones before. */
export function drawCells(
  leaflet: LeafletMap,
  cells: readonly Cell[],
  precision: number,
): void {
  const renderer = cellRenderer(leaflet.map);
  leaflet.cells.clearLayers();
  for (const cell of cells) {
    const square = L.rectangle(boundsOf(cellBox(cell, precision)), {
      ...cellStyle(leaflet.colors, cell.count),
      renderer,
    });
    leaflet.cellOf.set(square, { cell, precision });
    leaflet.cells.addLayer(square);
  }
}

/** Frame a cell: "Aproximar", and it becomes its smaller cells. */
export function frameCell(leaflet: LeafletMap, cell: Cell, precision: number): void {
  leaflet.map.fitBounds(boundsOf(cellBox(cell, precision)));
}
