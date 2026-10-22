/**
 * The parts of the map screen around the Leaflet (RFC-040 sections 5.3 to
 * 5.5): the legend, the balloon of a cell, the list, and the amber notices.
 * Their words are `words.ts`, and the tests read them there.
 */

import { useEffect, useLayoutEffect } from "react";

import type { MapCells } from "../api/queries";
import type { SearchQuery } from "../search/params";
import { type Cell, CLASS_LABELS } from "./cells";
import styles from "./MapScreen.module.css";
import { cellSizeText, MAX_CELL_PRECISION } from "./precision";
import {
  balloonCountLines,
  centreText,
  hiddenText,
  LEGEND_TITLE,
  legendSizeText,
  LIST_TITLE,
  NONE_HERE_TEXT,
  photosText,
  radiusLine,
  searchButtonText,
} from "./words";

const SWATCHES = [
  styles.swatch1,
  styles.swatch2,
  styles.swatch3,
  styles.swatch4,
  styles.swatch5,
];

/** In the corner below on the left: "Fotos por célula", the five classes and
 * the size of the cells, with why when the route coarsened them. */
export function Legend({ requested, applied }: { requested: number; applied: number }) {
  return (
    <div className={styles.legend}>
      <p className={styles.legendTitle}>{LEGEND_TITLE}</p>
      <ul className={styles.legendClasses}>
        {CLASS_LABELS.map((label, index) => (
          <li key={label} className={styles.legendClass}>
            <span
              className={`${styles.swatch} ${SWATCHES[index]}`}
              aria-hidden="true"
            />
            {label}
          </li>
        ))}
      </ul>
      <p className={styles.legendSize}>{legendSizeText(requested, applied)}</p>
    </div>
  );
}

export interface BalloonProps {
  readonly cell: Cell;
  readonly precision: number;
  /** The search the map carries; `null` opens the home screen. */
  readonly query: SearchQuery | null;
  readonly photoName: string | null;
  /** The radius that covers the cell; `null` until `min_radius_m` is known. */
  readonly radiusM: number | null;
  /** A click here is not "outside": the map and its panels, whose clicks the
   * map handles itself. */
  readonly area: HTMLElement | null;
  readonly onSearch: () => void;
  readonly onZoom: () => void;
  readonly onClose: () => void;
  /** After every render: the popup measures its content again. */
  readonly onLayout: () => void;
}

/**
 * Over the clicked cell (section 5.4): the count, "Buscar perto daqui" with the
 * search the map carries and the radius below it, and "Aproximar" while the
 * precision is below 3. It closes on `Esc`, on a click outside, and when other
 * cells arrive -- which the screen does by drawing no balloon for them.
 */
export function Balloon({
  cell,
  precision,
  query,
  photoName,
  radiusM,
  area,
  onSearch,
  onZoom,
  onClose,
  onLayout,
}: BalloonProps) {
  const [count, size] = balloonCountLines(cell.count, precision);

  useLayoutEffect(() => onLayout());

  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    // A press inside the popup never gets here: Leaflet stops it.
    const outside = (event: MouseEvent) => {
      if (area === null || !area.contains(event.target as Node)) {
        onClose();
      }
    };
    document.addEventListener("keydown", escape);
    document.addEventListener("mousedown", outside);
    return () => {
      document.removeEventListener("keydown", escape);
      document.removeEventListener("mousedown", outside);
    };
  }, [area, onClose]);

  return (
    <div className={styles.balloon} role="group" aria-label={`${count} ${size}`}>
      <p className={styles.balloonCount}>{count}</p>
      <p className={styles.balloonSize}>{size}</p>
      <button
        type="button"
        className={styles.balloonSearch}
        onClick={onSearch}
        disabled={radiusM === null}
      >
        {searchButtonText(query, photoName)}
      </button>
      {radiusM !== null && (
        <p className={styles.balloonRadius}>{radiusLine(radiusM)}</p>
      )}
      {precision < MAX_CELL_PRECISION && (
        <button type="button" className={styles.balloonZoom} onClick={onZoom}>
          Aproximar
        </button>
      )}
    </div>
  );
}

export interface CellListProps {
  readonly cells: readonly Cell[];
  readonly precision: number;
  readonly canSearch: boolean;
  readonly onOpen: (cell: Cell) => void;
  readonly onSearch: (cell: Cell) => void;
  readonly onClose: () => void;
}

/** Beside the map: the 20 fullest cells of the visible area, the keyboard's
 * and the screen reader's way to a cell (section 5.5). */
export function CellList({
  cells,
  precision,
  canSearch,
  onOpen,
  onSearch,
  onClose,
}: CellListProps) {
  return (
    <aside className={styles.list} aria-label={LIST_TITLE}>
      <div className={styles.listHeader}>
        <h2 className={styles.listTitle}>{LIST_TITLE}</h2>
        <button
          type="button"
          className={styles.listClose}
          onClick={onClose}
          aria-label="Fechar a lista"
        >
          ×
        </button>
      </div>
      {cells.length === 0 ? (
        <p className={styles.listEmpty}>{NONE_HERE_TEXT}</p>
      ) : (
        <ol className={styles.listRows}>
          {cells.map((cell) => (
            <li key={`${cell.latitude},${cell.longitude}`} className={styles.listRow}>
              <button
                type="button"
                className={styles.listCell}
                onClick={() => onOpen(cell)}
              >
                <span className={styles.listCount}>{photosText(cell.count)}</span>
                <span className={styles.listMeta}>
                  célula de {cellSizeText(precision)} ·{" "}
                  {centreText(cell.latitude, cell.longitude)}
                </span>
              </button>
              <button
                type="button"
                className={styles.listSearch}
                onClick={() => onSearch(cell)}
                disabled={!canSearch}
              >
                Buscar perto daqui
              </button>
            </li>
          ))}
        </ol>
      )}
    </aside>
  );
}

/** Above the map, as the search's are above the grid: over the whole
 * collection under the filters, never "nesta área" (section 4.1). */
export function Notices({
  cells,
  onClearDates,
}: {
  cells: MapCells;
  onClearDates: () => void;
}) {
  const date = hiddenText(cells.excluded_unknown_date, "date");
  const position = hiddenText(cells.excluded_unknown_position, "position");
  if (date === null && position === null) {
    return null;
  }
  return (
    <div className={styles.notices}>
      {date !== null && (
        <div className={styles.notice}>
          <p className={styles.noticeText}>{date}</p>
          <button type="button" className={styles.noticeAction} onClick={onClearDates}>
            Tirar o filtro de datas
          </button>
        </div>
      )}
      {position !== null && (
        <div className={styles.notice}>
          <p className={styles.noticeText}>{position}</p>
        </div>
      )}
    </div>
  );
}
