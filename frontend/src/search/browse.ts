/**
 * What the listing by date says, and how its pages add up (RFC-044 section 6),
 * as pure functions: the tests read them without a DOM.
 *
 * **The dates come from the text**, never through a `Date`: the capture date is
 * the camera's local time, and `new Date("2018-11-04T00:15:00")` would move an
 * hour that fell in a daylight-saving jump of the window's zone (RFC-038
 * section 2.5).
 */

import type { ImageDetail } from "../api/client";
import { PAGE_SIZE } from "./actions";
import type { BrowsePage, BrowseSummary } from "./api";
import { formatDate } from "./dates";
import type { BrowseOrder } from "./params";
import { formatRadius } from "./radius";
import type { Corner } from "./ResultGrid";

/** The action beside the question that exists, never in its place (6.2). */
export const BROWSE_ACTION_TEXT = "Ver todas as fotos daqui, por data";

/** The bar of the listing: a text and Enter go back to the ranking. */
export const BROWSE_PROMPT = "Descreva o que procurar perto daqui…";

export const ORDER_LABELS: Readonly<Record<BrowseOrder, string>> = {
  newest: "Mais recentes primeiro",
  oldest: "Mais antigas primeiro",
};

export const BROWSE_FOOTNOTE =
  "Ordenado pela data de captura, como o relógio da câmera a gravou.";

export const DIVIDER_NOTE =
  "O arquivo não diz quando foram tiradas. Ficam no fim, na ordem das pastas.";

export const RESTART = "Recomeçar";
export const READING_MORE = "Lendo…";

const COUNT = new Intl.NumberFormat("pt-BR");
const CAPTURED_AT = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/;

function photos(count: number): string {
  return count === 1 ? "1 foto" : `${COUNT.format(count)} fotos`;
}

/** The day of a capture date, `03/01/2015`. */
function dayOf(capturedAt: string): string {
  return formatDate(capturedAt.slice(0, 10));
}

/**
 * The header, where the search writes "14 resultados · 0,13 s":
 * `6.102 fotos daqui, de 03/01/2015 a 02/10/2026 · 305 sem data`. Without a
 * dated photo, no span; without an undated one, no last part.
 */
export function browseHeaderText(summary: BrowseSummary): string {
  if (summary.total === 0) {
    return "Nenhuma foto daqui";
  }
  let text = `${photos(summary.total)} daqui`;
  if (summary.oldest !== null && summary.newest !== null) {
    const oldest = dayOf(summary.oldest);
    const newest = dayOf(summary.newest);
    text += oldest === newest ? `, em ${oldest}` : `, de ${oldest} a ${newest}`;
  }
  if (summary.undated > 0) {
    text += ` · ${COUNT.format(summary.undated)} sem data`;
  }
  return text;
}

/** Beside "Mostrar mais": `140 de 6.102`. */
export function progressText(shown: number, total: number): string {
  return `${COUNT.format(shown)} de ${COUNT.format(total)}`;
}

/** The divider's title: `Sem data de captura · 305 fotos`. */
export function dividerTitle(undated: number): string {
  return `Sem data de captura · ${photos(undated)}`;
}

/** No photo in the circle: `Nenhuma foto até 1 km daqui.`, and with other
 * filters, `…com esses filtros`. */
export function emptyBrowseText(radiusM: number, filtered: boolean): string {
  const where = `Nenhuma foto até ${formatRadius(radiusM)} daqui`;
  return filtered ? `${where} com esses filtros.` : `${where}.`;
}

/** `2018-07-14T15:32:05` as `14/07/2018 15:32`, from the text alone. */
export function cardDateText(capturedAt: string): string {
  const parts = CAPTURED_AT.exec(capturedAt);
  if (parts === null) {
    return capturedAt;
  }
  return `${parts[3]}/${parts[2]}/${parts[1]} ${parts[4]}:${parts[5]}`;
}

/** The date in the corner of a card, where the search writes the cosine, and
 * where it came from; `null` -- nothing
 * in the corner -- for a photo with no date. */
export function dateCorner(
  result: Pick<ImageDetail, "captured_at" | "capture_source">,
): Corner | null {
  const capturedAt = result.captured_at;
  if (capturedAt === null) {
    return null;
  }
  const text = cardDateText(capturedAt);
  const [day, time] = text.split(" ");
  const when = `${day} às ${time}`;
  const title =
    result.capture_source === "exif_digitized"
      ? `Digitalizada em ${when}`
      : `Tirada em ${when}, pelo relógio da câmera`;
  return { text, title };
}

// ---------------------------------------------------------------------------
// The pages

/** The first page is the search's 40; every one after it is the
 * `max_search_limit` of `/capabilities`, 100 (section 6.3). */
export function browsePageLimit(after: string | null, maxLimit: number): number {
  return after === null ? PAGE_SIZE : maxLimit;
}

/** The pages on the screen, one after the other: "Mostrar mais" appends. */
export function flattenPages(
  pages: readonly Pick<BrowsePage, "results">[],
): ImageDetail[] {
  return pages.flatMap((page) => page.results);
}

/** Whether "Mostrar mais" is drawn: the last page said there is a next one. */
export function canBrowseMore(
  pages: readonly Pick<BrowsePage, "nextAfter">[],
): boolean {
  const last = pages.at(-1);
  return last !== undefined && last.nextAfter !== null;
}

/** The card the divider goes before: the first with no date. The undated
 * close the list in both directions, so this is where they start. */
export function firstUndatedId(
  results: readonly Pick<ImageDetail, "id" | "captured_at">[],
): string | null {
  return results.find((result) => result.captured_at === null)?.id ?? null;
}
