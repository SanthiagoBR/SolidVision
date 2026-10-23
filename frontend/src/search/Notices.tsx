/**
 * The amber notices above the grid (RFC-038 section 5.3): how many photos a
 * filter left out, over the whole collection and not over the page.
 *
 * Shared by the search and the listing by date (RFC-044 section 6.3). The date
 * notice has "Tirar o filtro de datas" in both: in the listing it brings the
 * undated back, to the end of the list. The position notice has "Tirar o filtro
 * de posição" in the search only: the listing lists a place, and without the
 * circle there is no listing.
 */

import styles from "./SearchScreen.module.css";

const COUNT = new Intl.NumberFormat("pt-BR");

export function excludedText(count: number, reason: "date" | "position"): string {
  const photos = count === 1 ? "1 foto ficou" : `${COUNT.format(count)} fotos ficaram`;
  const have = count === 1 ? "tem" : "têm";
  return reason === "date"
    ? `${photos} de fora porque não ${have} data de captura.`
    : `${photos} de fora porque não ${have} posição.`;
}

export interface NoticesProps {
  readonly excludedUnknownDate: number | null;
  readonly excludedUnknownPosition: number | null;
  readonly onClearDates: () => void;
  /** Absent: the position notice has no button. */
  readonly onClearPosition?: () => void;
}

export function Notices({
  excludedUnknownDate,
  excludedUnknownPosition,
  onClearDates,
  onClearPosition,
}: NoticesProps) {
  const date = excludedUnknownDate ?? 0;
  const position = excludedUnknownPosition ?? 0;
  if (date === 0 && position === 0) {
    return null;
  }
  return (
    <div className={styles.notices}>
      {date > 0 && (
        <div className={styles.notice}>
          <p className={styles.noticeText}>{excludedText(date, "date")}</p>
          <button type="button" className={styles.noticeAction} onClick={onClearDates}>
            Tirar o filtro de datas
          </button>
        </div>
      )}
      {position > 0 && (
        <div className={styles.notice}>
          <p className={styles.noticeText}>{excludedText(position, "position")}</p>
          {onClearPosition !== undefined && (
            <button
              type="button"
              className={styles.noticeAction}
              onClick={onClearPosition}
            >
              Tirar o filtro de posição
            </button>
          )}
        </div>
      )}
    </div>
  );
}
