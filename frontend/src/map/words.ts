/**
 * What the map screen says (RFC-040 sections 5.3 to 5.7), as pure functions:
 * the tests read them without the Leaflet, and without a DOM.
 *
 * **The count is never in the colour alone**: it is in the hint, in the
 * balloon and in the list. And **the two notices count over the whole
 * collection under the filters, not over the area** (section 4.1): their text
 * never says "nesta área".
 */

import type { SearchQuery } from "../search/params";
import { formatRadius } from "../search/radius";
import { cellSizeText } from "./precision";

const COUNT = new Intl.NumberFormat("pt-BR");

/** `1 foto`, `37 fotos`, `1.234 fotos`. */
export function photosText(count: number): string {
  return count === 1 ? "1 foto" : `${COUNT.format(count)} fotos`;
}

/** The hint under the pointer: the count, and below it, smaller, the cell. */
export function hintLines(count: number, precision: number): [string, string] {
  return [photosText(count), `célula de ${cellSizeText(precision)}`];
}

const DEGREES = new Intl.NumberFormat("pt-BR", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** The centre of a cell with two places, as the list writes it: `26,32° S,
 * 48,82° O`. The letters keep the decimal commas from reading as a list. */
export function centreText(latitude: number, longitude: number): string {
  const lat = `${DEGREES.format(Math.abs(latitude))}° ${latitude < 0 ? "S" : "N"}`;
  const lon = `${DEGREES.format(Math.abs(longitude))}° ${longitude < 0 ? "O" : "L"}`;
  return `${lat}, ${lon}`;
}

export const LIST_TITLE = "Células com mais fotos";

/** The balloon's first lines: `37 fotos` and `nesta célula de ~1 km`. */
export function balloonCountLines(count: number, precision: number): [string, string] {
  return [photosText(count), `nesta célula de ${cellSizeText(precision)}`];
}

/** What the search the map carries is about: a text in quotes, or the photo
 * the "parecidas" start from. `photoName` is the indexed photo's file name,
 * when it is known. */
function subject(query: SearchQuery, photoName: string | null): string {
  switch (query.kind) {
    case "text":
      return `“${query.q}”`;
    case "similar":
      return `parecidas com ${photoName ?? "uma foto"}`;
    case "image":
      return "parecidas com a foto solta";
  }
}

/** `Buscar “telhado azul” perto daqui`, `Buscar parecidas com DJI_0042.JPG
 * perto daqui`, or, with no search, `Buscar perto daqui`. */
export function searchButtonText(
  query: SearchQuery | null,
  photoName: string | null,
): string {
  return query === null
    ? "Buscar perto daqui"
    : `Buscar ${subject(query, photoName)} perto daqui`;
}

/** Below the button: `num raio de 10 km do centro da célula`. */
export function radiusLine(radiusM: number): string {
  return `num raio de ${formatRadius(radiusM)} do centro da célula`;
}

/** The line above the map, when it carries a search (section 5.5). */
export function carriedSearchText(
  query: SearchQuery,
  photoName: string | null,
): string {
  return (
    `Um clique numa célula busca ${subject(query, photoName)} perto dali. ` +
    "O mapa mostra todas as fotos com posição, e não só as dessa busca."
  );
}

export const LEGEND_TITLE = "Fotos por célula";

/** `Células de ~11 km`; when the route coarsened the grid, why. */
export function legendSizeText(requested: number, applied: number): string {
  if (applied < requested) {
    return (
      `Células de ${cellSizeText(applied)}, maiores que as deste zoom: a área tem ` +
      `fotos em lugares demais para as de ${cellSizeText(requested)}.`
    );
  }
  return `Células de ${cellSizeText(applied)}`;
}

/** The amber notice of the photos the map cannot draw; `null` for none. */
export function hiddenText(
  count: number | null | undefined,
  reason: "date" | "position",
): string | null {
  if (count === null || count === undefined || count <= 0) {
    return null;
  }
  const photos =
    count === 1 ? "1 foto não aparece" : `${COUNT.format(count)} fotos não aparecem`;
  const have = count === 1 ? "tem" : "têm";
  return reason === "date"
    ? `${photos} no mapa porque não ${have} data de captura.`
    : `${photos} no mapa porque não ${have} posição.`;
}

export const LOADING_TEXT = "Lendo o mapa…";
export const NONE_INDEXED_TEXT = "Nenhuma foto indexada ainda.";
export const NONE_HERE_TEXT = "Nenhuma foto nesta área.";
export const NONE_HERE_FILTERED_TEXT = "Nenhuma foto nesta área com esses filtros.";

export interface MapStateInput {
  /** Cells are on the map: a response arrived. */
  readonly answered: boolean;
  /** A request is out, for more than 300 ms. */
  readonly late: boolean;
  /** A request is out at all. */
  readonly fetching: boolean;
  /** The sum of `indexed_images`, or `null` before `/devices` answers. */
  readonly indexedTotal: number | null;
  /** The cells that meet the visible area. */
  readonly visibleCells: number;
  /** A device or a date is chosen. */
  readonly filtered: boolean;
}

export type MapState =
  | { readonly kind: "loading"; readonly where: "centre" | "corner" }
  | { readonly kind: "empty"; readonly text: string }
  | null;

/**
 * What the screen shows over the map:
 *
 *     when                                       the screen shows
 *     the first response, after 300 ms           "Lendo o mapa…" over the map
 *     a later request, after 300 ms              the cells of before, and
 *                                                "Lendo o mapa…" in a corner
 *     no photo indexed at all                    "Nenhuma foto indexada ainda."
 *     no cell in the visible area                "Nenhuma foto nesta área.",
 *                                                "…com esses filtros"
 *
 * No empty message while a request is out: the cells it answers are not on
 * the map yet. The 300 ms is a choice.
 */
export function mapState(input: MapStateInput): MapState {
  if (!input.answered) {
    return input.late ? { kind: "loading", where: "centre" } : null;
  }
  if (input.fetching) {
    return input.late ? { kind: "loading", where: "corner" } : null;
  }
  if (input.visibleCells > 0) {
    return null;
  }
  if (input.indexedTotal === 0) {
    return { kind: "empty", text: NONE_INDEXED_TEXT };
  }
  return {
    kind: "empty",
    text: input.filtered ? NONE_HERE_FILTERED_TEXT : NONE_HERE_TEXT,
  };
}

export const BAD_AREA = "Não deu para ler o mapa desta área.";
export const MAP_FAILED = "Não deu para ler o mapa agora.";

/**
 * The sentence for a failed request (section 5.7); `null` for none, and for no
 * answer at all, which the shell's "Reconectando…" covers, with the cells of
 * before still on the map.
 *
 * A 400 or a 422 is a defect of the page -- it cuts the box to the bands, never
 * inverts it, never sends a negative precision or a malformed `device_id` --
 * and its status goes to the console, and from there to `shell.log`, without
 * the `detail`.
 */
export function mapErrorMessage(status: number | null): string | null {
  if (status === null || status === 0) {
    return null;
  }
  return status === 400 || status === 422 ? BAD_AREA : MAP_FAILED;
}
