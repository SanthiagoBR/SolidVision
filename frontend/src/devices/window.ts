/**
 * The rows of a long list that are in the DOM (RFC-039 sections 2.7 and 5.6).
 *
 * A folder can hold 5 000 folders (RFC-031 section 8.3), and 5 000 rows in the
 * DOM are a page that stops answering while they are laid out. The list draws
 * rows of a fixed height, and only the visible ones plus ten on each side.
 * The height, the overscan and the threshold of the filter are choices.
 *
 * No dependency: a function of the scroll, the height of the viewport and the
 * count, tested without a DOM.
 */

/** The height of a row, in pixels: what the CSS of the list draws. */
export const ROW_HEIGHT = 44;
/** Rows kept on each side of the visible ones. */
export const OVERSCAN = 10;

export interface Range {
  /** The first row in the DOM. */
  readonly start: number;
  /** One past the last. */
  readonly end: number;
}

export function visibleRange(
  count: number,
  scrollTop: number,
  viewportHeight: number,
  rowHeight: number = ROW_HEIGHT,
  overscan: number = OVERSCAN,
): Range {
  if (count <= 0 || rowHeight <= 0) {
    return { start: 0, end: 0 };
  }
  const top = Math.max(0, scrollTop);
  const height = Math.max(0, viewportHeight);
  const first = Math.min(count, Math.floor(top / rowHeight));
  const last = Math.min(count, Math.ceil((top + height) / rowHeight));
  return {
    start: Math.max(0, first - overscan),
    end: Math.min(count, Math.max(last, first + 1) + overscan),
  };
}
