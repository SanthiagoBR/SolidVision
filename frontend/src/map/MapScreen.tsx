/**
 * `/mapa`: where the photos are (RFC-040 section 5).
 *
 * The cells of `GET /images/map` over the outline of the states and the
 * OpenStreetMap's tiles, at the precision of the zoom, drawn at the precision
 * the response says it applied; the search's device and date filters; a
 * balloon over a clicked cell, with "Buscar perto daqui"; the notices of the
 * photos the map cannot draw; and a list of the fullest cells, for the
 * keyboard.
 *
 *     when                                       the page asks the API
 *     the screen opens                           for the box, at the zoom's
 *                                                precision
 *     a drag or a zoom ends, and the visible     again; a short drag asks
 *     area left the last box, or the precision   nothing
 *     changed
 *     a filter changes                           again
 *     a job reaches a final state                again: `sync.ts` invalidates
 *                                                the map
 *     the window comes back, or time passes      nothing
 *
 * **State comes from Leaflet's events, and nothing reads a ref while
 * rendering**: the end of a move sets the viewport and the area asked for, a
 * click sets the balloon, a screen of failed tiles pauses the layer. The
 * request is the area and the URL's filters, so a filter changes it during
 * the render; and the balloon belongs to the cells it was opened on, so other
 * cells arriving close it without an effect.
 */

import "leaflet/dist/leaflet.css";

import * as L from "leaflet";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useNavigate, useSearchParams } from "react-router";

import { ApiError } from "../api/client";
import { type MapCells, useDevices, useMapCells } from "../api/queries";
import { useSearchActions } from "../search/actions";
import { useCapabilities, useImage } from "../search/api";
import {
  type Filters,
  hasFilters,
  type Point,
  type SearchQuery,
} from "../search/params";
import { fileName } from "../search/paths";
import { FilterPills } from "../search/SearchControls";
import {
  type Box,
  mapFilters,
  type MapFilters,
  type MapRequest,
  planRequest,
} from "./box";
import { type Cell, cellRadius, fullestCells, visibleCells } from "./cells";
import {
  boxOf,
  CELL_HOVER_WEIGHT,
  CELL_WEIGHT,
  createMap,
  drawCells,
  FIT_PADDING,
  frameCell,
  type LeafletMap,
} from "./leaflet";
import {
  AddressControl,
  Balloon,
  CellList,
  CoordinatesControl,
  Legend,
  Notices,
} from "./MapParts";
import styles from "./MapScreen.module.css";
import {
  countTile,
  NEW_SCREEN,
  OFFLINE_TEXT,
  pausesAt,
  readOsmChoice,
  windowStorage,
  writeOsmChoice,
} from "./tiles";
import { finishMapGesture, startMapGesture } from "./timing";
import { mapHref, readMapUrl, roundView, viewKey } from "./url";
import {
  carriedSearchText,
  hintLines,
  LOADING_TEXT,
  mapErrorMessage,
  mapState,
} from "./words";

interface Viewport {
  readonly visible: Box;
  readonly zoom: number;
}

interface Area {
  readonly box: Box;
  readonly precision: number;
}

interface BalloonState {
  readonly cell: Cell;
  readonly precision: number;
  readonly at: L.LatLng;
  /** The answer it was opened on: other cells arriving close it. */
  readonly cells: MapCells;
}

const LATE_MS = 300;
/** The zoom "Ir para as coordenadas" lands on, unless the map is already
 * closer: ~100 m cells (precision 3), close enough to a property. */
const COORDINATES_ZOOM = 14;

export default function MapScreen() {
  const [params] = useSearchParams();
  const url = useMemo(() => readMapUrl(params), [params]);
  const filtersKey = JSON.stringify(mapFilters(url.filters));
  const filters = useMemo(() => JSON.parse(filtersKey) as MapFilters, [filtersKey]);
  const navigate = useNavigate();
  const devices = useDevices();
  const capabilities = useCapabilities();
  const minRadius = capabilities.data?.min_radius_m ?? null;
  const photoName = usePhotoName(url.query);
  const search = useSearchActions({
    query: url.query,
    filters: url.filters,
    photo: null,
  });

  const container = useRef<HTMLDivElement>(null);
  const leafletRef = useRef<LeafletMap | null>(null);
  const popupRef = useRef<L.Popup | null>(null);
  /** The framing the URL has, or is about to have: the URL's own changes --
   * back, forward, "Mapa" again -- are the others. */
  const lastView = useRef("");
  const firstSync = useRef(true);
  /** A button of the balloon or of the list is taking the screen elsewhere:
   * the map no longer writes its framing to the URL. */
  const leaving = useRef(false);

  const [viewport, setViewport] = useState<Viewport | null>(null);
  const [area, setArea] = useState<Area | null>(null);
  const request = useMemo<MapRequest | null>(
    () => (area === null ? null : { ...area, filters }),
    [area, filters],
  );
  const answer = useMapCells(request);
  const data = answer.data;
  const late = useLate(answer.isFetching);

  const [balloon, setBalloon] = useState<BalloonState | null>(null);
  const shown = balloon !== null && balloon.cells === data ? balloon : null;
  const [popupContent] = useState(() => document.createElement("div"));
  const [body, setBody] = useState<HTMLDivElement | null>(null);
  const [osmOn, setOsmOn] = useState(() => readOsmChoice(windowStorage()));
  const [paused, setPaused] = useState(false);
  const [listOpen, setListOpen] = useState(false);

  // What Leaflet's handlers read: the last render's values.
  const latest = useRef({ url, request, data, navigate });
  useEffect(() => {
    latest.current = { url, request, data, navigate };
  });

  // The map, once: its layers, its handlers, and the first request.
  useEffect(() => {
    const element = container.current;
    if (element === null) {
      return;
    }
    const made = createMap(element, latest.current.url.view);
    leafletRef.current = made;
    lastView.current = viewKey(latest.current.url.view);
    const { map } = made;

    const viewChanged = (gesture: boolean) => {
      if (leaving.current) {
        return;
      }
      const visible = boxOf(map.getBounds());
      const zoom = map.getZoom();
      setViewport({ visible, zoom });
      const centre = map.getCenter();
      const view = roundView({ lat: centre.lat, lon: centre.lng }, zoom);
      const key = viewKey(view);
      const now = latest.current;
      if (key !== lastView.current) {
        lastView.current = key;
        now.navigate(mapHref({ ...now.url, view }), { replace: true });
      }
      const next = planRequest(now.request, visible, zoom, mapFilters(now.url.filters));
      if (next !== now.request) {
        if (gesture) {
          startMapGesture();
        }
        latest.current = { ...now, request: next };
        setArea({ box: next.box, precision: next.precision });
      }
    };
    map.on("moveend", () => viewChanged(true));
    // The first framing is the screen's opening, whose gesture -- the click on
    // "Mapa" -- started before this part of the build loaded.
    queueMicrotask(() => {
      if (leafletRef.current === made) {
        viewChanged(false);
      }
    });

    // The hint, the 2 px outline, and the balloon.
    made.cells.bindTooltip((layer) => hintElement(made, layer), {
      sticky: true,
      direction: "top",
      offset: [0, -10],
      opacity: 1,
      className: styles.hint,
    });
    made.cells.on("mouseover", (event) => {
      const square = event.propagatedFrom as L.Path;
      square.setStyle({ weight: CELL_HOVER_WEIGHT });
      square.bringToFront();
    });
    made.cells.on("mouseout", (event) => {
      (event.propagatedFrom as L.Path).setStyle({ weight: CELL_WEIGHT });
    });
    made.cells.on("click", (event: L.LeafletMouseEvent) => {
      const found = made.cellOf.get(event.propagatedFrom as L.Layer);
      const cells = latest.current.data;
      if (found === undefined || cells === undefined) {
        return;
      }
      made.cells.closeTooltip();
      setBalloon({ ...found, at: event.latlng, cells });
    });

    // The pause: a screen of tiles with none loaded and some failed.
    let screen = NEW_SCREEN;
    made.tiles.on("loading", () => {
      screen = countTile(screen, "loading");
    });
    made.tiles.on("tileload", () => {
      screen = countTile(screen, "tileload");
    });
    made.tiles.on("tileerror", () => {
      screen = countTile(screen, "tileerror");
    });
    made.tiles.on("load", () => {
      if (pausesAt(screen)) {
        setPaused(true);
      }
    });

    // The list beside it, and the window, change the map's size.
    const resize = new ResizeObserver(() => map.invalidateSize());
    resize.observe(element);
    return () => {
      resize.disconnect();
      map.remove();
      if (leafletRef.current === made) {
        leafletRef.current = null;
      }
    };
  }, []);

  // Back, forward and "Mapa" again move the map to the URL's framing.
  const urlView = viewKey(url.view);
  useEffect(() => {
    const made = leafletRef.current;
    if (made === null) {
      return;
    }
    if (firstSync.current) {
      // The map was just made with it.
      firstSync.current = false;
      return;
    }
    if (urlView === lastView.current) {
      return;
    }
    lastView.current = urlView;
    const view = latest.current.url.view;
    if (view === null) {
      made.map.fitBounds(made.brazil, { padding: FIT_PADDING });
    } else {
      made.map.setView([view.center.lat, view.center.lon], view.zoom);
    }
  }, [urlView]);

  // The OpenStreetMap layer: on by choice, and off while paused.
  const showTiles = osmOn && !paused;
  useEffect(() => {
    const made = leafletRef.current;
    if (made === null) {
      return;
    }
    if (showTiles) {
      made.tiles.addTo(made.map);
    } else {
      made.tiles.remove();
    }
  }, [showTiles]);

  useEffect(() => {
    const resume = () => setPaused(false);
    window.addEventListener("online", resume);
    return () => window.removeEventListener("online", resume);
  }, []);

  // The cells of each answer, and the mark of section 5.6 two frames later.
  useEffect(() => {
    const made = leafletRef.current;
    if (made === null || data === undefined) {
      return;
    }
    drawCells(made, data.cells, data.precision_applied);
    const painted = {
      cells: data.cells.length,
      precisionRequested: data.precisionRequested,
      precisionApplied: data.precision_applied,
      receivedAt: data.receivedAt,
    };
    let second = 0;
    const first = requestAnimationFrame(() => {
      second = requestAnimationFrame(() =>
        finishMapGesture(painted, performance.now()),
      );
    });
    return () => {
      cancelAnimationFrame(first);
      cancelAnimationFrame(second);
    };
  }, [data]);

  // The balloon, in a Leaflet popup over the cell; React draws inside it.
  useEffect(() => {
    const made = leafletRef.current;
    if (made === null || shown === null) {
      return;
    }
    const popup = L.popup({
      closeButton: false,
      // Clear of the controls in the corner above, which are drawn over the map.
      autoPanPaddingTopLeft: [16, 56],
      autoPanPaddingBottomRight: [16, 16],
      minWidth: 220,
      maxWidth: 320,
      offset: [0, -6],
      className: styles.popup,
    })
      .setLatLng(shown.at)
      .setContent(popupContent);
    const closed = () => setBalloon(null);
    popup.on("remove", closed);
    popup.openOn(made.map);
    popupRef.current = popup;
    popupContent
      .querySelector<HTMLButtonElement>("button:not(:disabled)")
      ?.focus({ preventScroll: true });
    return () => {
      popup.off("remove", closed);
      popup.remove();
      if (popupRef.current === popup) {
        popupRef.current = null;
      }
    };
  }, [shown, popupContent]);

  const status =
    answer.error instanceof ApiError ? answer.error.status : answer.error ? -1 : null;
  useEffect(() => {
    if (status === 400 || status === 422) {
      // A defect of the page (section 5.7): the status, and not the detail.
      console.error(`O mapa respondeu ${status}`);
    }
  }, [status]);

  const closeBalloon = useCallback(() => setBalloon(null), []);
  const layoutBalloon = useCallback(() => popupRef.current?.update(), []);

  const onFilters = (next: Filters) => {
    startMapGesture();
    navigate(mapHref({ ...url, filters: next }));
  };
  // The balloon's auto-pan may be running when one of its buttons is clicked,
  // and its end would replace the URL with the map's framing after the
  // navigation began -- undoing it (RFC-044 section 16). So the map stops, and
  // stops writing, before the screen changes.
  const leave = () => {
    leaving.current = true;
    leafletRef.current?.map.stop();
  };
  const searchNear = (cell: Cell, precision: number) => {
    if (minRadius !== null) {
      leave();
      search.nearPoint(
        { lat: cell.latitude, lon: cell.longitude },
        cellRadius(cell, precision, minRadius),
      );
    }
  };
  const browseNear = (cell: Cell, precision: number) => {
    if (minRadius !== null) {
      leave();
      search.browseNear(
        { lat: cell.latitude, lon: cell.longitude },
        cellRadius(cell, precision, minRadius),
      );
    }
  };
  const openFromList = (cell: Cell) => {
    const made = leafletRef.current;
    if (made === null || data === undefined) {
      return;
    }
    const at = L.latLng(cell.latitude, cell.longitude);
    made.map.panTo(at, { animate: false });
    setBalloon({ cell, precision: data.precision_applied, at, cells: data });
  };
  const zoomInto = (cell: Cell, precision: number) => {
    const made = leafletRef.current;
    setBalloon(null);
    if (made !== null) {
      frameCell(made, cell, precision);
    }
  };
  const toggleOsm = (on: boolean) => {
    writeOsmChoice(windowStorage(), on);
    setOsmOn(on);
    setPaused(false);
  };
  const goToCoordinates = (point: Point) => {
    const made = leafletRef.current;
    if (made === null) {
      return;
    }
    setBalloon(null);
    const zoom = Math.min(
      Math.max(made.map.getZoom(), COORDINATES_ZOOM),
      made.map.getMaxZoom(),
    );
    made.map.setView([point.lat, point.lon], zoom);
  };

  const indexedTotal =
    devices.data === undefined
      ? null
      : devices.data.reduce((sum, device) => sum + device.indexed_images, 0);
  const inView =
    data !== undefined && viewport !== null
      ? visibleCells(data.cells, data.precision_applied, viewport.visible).length
      : 0;
  const state = mapState({
    answered: data !== undefined,
    late,
    fetching: answer.isFetching,
    indexedTotal,
    visibleCells: inView,
    filtered: hasFilters(url.filters),
  });
  const errorText = mapErrorMessage(status === -1 ? 500 : status);
  const listed = useMemo(
    () =>
      data !== undefined && viewport !== null
        ? fullestCells(data.cells, data.precision_applied, viewport.visible)
        : [],
    [data, viewport],
  );

  return (
    <div className={styles.screen}>
      <FilterPills filters={url.filters} onFilters={onFilters} />

      {url.query !== null && (
        <div className={styles.carried}>
          <p className={styles.carriedText}>
            {carriedSearchText(url.query, photoName)}
          </p>
          <button
            type="button"
            className={styles.link}
            onClick={() => navigate(mapHref({ ...url, query: null }))}
          >
            Esquecer a busca
          </button>
        </div>
      )}

      {data !== undefined && (
        <Notices
          cells={data}
          onClearDates={() => onFilters({ ...url.filters, from: null, to: null })}
        />
      )}
      {errorText !== null && (
        <p className={styles.error} role="alert">
          {errorText}
        </p>
      )}
      {paused && osmOn && (
        <div className={styles.offline} role="status">
          <p className={styles.offlineText}>{OFFLINE_TEXT}</p>
          <button
            type="button"
            className={styles.link}
            onClick={() => setPaused(false)}
          >
            Tentar de novo
          </button>
        </div>
      )}

      <div className={styles.body} ref={setBody}>
        <div className={styles.area}>
          <div ref={container} className={styles.map} />
          <div className={styles.controls}>
            {capabilities.data?.address_search === true && (
              <AddressControl onGo={goToCoordinates} />
            )}
            <CoordinatesControl onGo={goToCoordinates} />
            <label className={styles.control}>
              <input
                type="checkbox"
                checked={osmOn}
                onChange={(event) => toggleOsm(event.target.checked)}
              />
              Fundo do OpenStreetMap
            </label>
            <button
              type="button"
              className={listOpen ? styles.controlOn : styles.control}
              aria-pressed={listOpen}
              onClick={() => setListOpen(!listOpen)}
            >
              Lista
            </button>
          </div>
          {state?.kind === "loading" && (
            <p
              className={
                state.where === "centre" ? styles.loadingCentre : styles.loadingCorner
              }
              role="status"
            >
              {LOADING_TEXT}
            </p>
          )}
          {state?.kind === "empty" && (
            <p className={styles.empty} role="status">
              {state.text}
            </p>
          )}
          {data !== undefined && (
            <Legend
              requested={data.precisionRequested}
              applied={data.precision_applied}
            />
          )}
        </div>
        {listOpen && data !== undefined && (
          <CellList
            cells={listed}
            precision={data.precision_applied}
            canSearch={minRadius !== null}
            onOpen={openFromList}
            onSearch={(cell) => searchNear(cell, data.precision_applied)}
            onClose={() => setListOpen(false)}
          />
        )}
      </div>

      {shown !== null &&
        createPortal(
          <Balloon
            cell={shown.cell}
            precision={shown.precision}
            query={url.query}
            photoName={photoName}
            radiusM={
              minRadius === null
                ? null
                : cellRadius(shown.cell, shown.precision, minRadius)
            }
            area={body}
            onSearch={() => searchNear(shown.cell, shown.precision)}
            onBrowse={() => browseNear(shown.cell, shown.precision)}
            onZoom={() => zoomInto(shown.cell, shown.precision)}
            onClose={closeBalloon}
            onLayout={layoutBalloon}
          />,
          popupContent,
        )}
    </div>
  );
}

/** The hint of the cell under the pointer: `37 fotos`, and below it, smaller,
 * `célula de ~1 km`. Built as elements: the tooltip takes no HTML of ours. */
function hintElement(made: LeafletMap, layer: L.Layer): HTMLElement {
  const element = document.createElement("span");
  const found = made.cellOf.get(layer);
  if (found !== undefined) {
    const [count, size] = hintLines(found.cell.count, found.precision);
    const strong = document.createElement("strong");
    strong.textContent = count;
    const small = document.createElement("small");
    small.textContent = size;
    element.append(strong, small);
  }
  return element;
}

/** The file name of the photo a "parecidas" starts from, once it is read. */
function usePhotoName(query: SearchQuery | null): string | null {
  const image = useImage(query?.kind === "similar" ? query.imageId : null);
  return image.data !== undefined ? fileName(image.data.relative_path) : null;
}

/** Whether a request has been out for more than 300 ms. */
function useLate(active: boolean): boolean {
  const [late, setLate] = useState(false);
  useEffect(() => {
    if (!active) {
      return;
    }
    const timer = setTimeout(() => setLate(true), LATE_MS);
    return () => {
      clearTimeout(timer);
      setLate(false);
    };
  }, [active]);
  return active && late;
}
