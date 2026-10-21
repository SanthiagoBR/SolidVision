/**
 * The one percentage of the interface (RFC-039 section 5.2; RFC-031 section
 * 12).
 *
 *     indexed_images / last_scan_file_count, with last_scan_at beside it
 *
 *     the device                     the card says
 *     no denominator                 nothing
 *     a denominator of zero          "o scan de 12/03 não achou fotos"
 *     more indexed than scanned      "o disco mudou desde o scan de 12/03": a
 *                                    disk whose photos were deleted after the
 *                                    scan, and no number above 100%
 *     between zero and one percent   "menos de 1% no scan de 12/03"
 *     otherwise                      "50% no scan de 12/03", rounded down, so
 *                                    that 100% means every file
 *
 * **Never without the date.** The denominator ages: "50%" alone is a confident
 * wrong number for whoever copied 10 000 photos to the disk yesterday (RFC-027
 * section 8). The job of the whole disk writes the denominator when its
 * listing ends (RFC-039 section 4.1), and a job of some folders never does.
 *
 * Pure: the card and the job panel render what this returns.
 */

import { dayText, formatCount, instant } from "./words";

export interface ScanFacts {
  readonly indexed_images: number;
  readonly last_scan_file_count: number | null;
  readonly last_scan_at: string | null;
}

export type ScanShare =
  | { readonly kind: "none" }
  | { readonly kind: "empty"; readonly text: string }
  | { readonly kind: "changed"; readonly text: string }
  | {
      readonly kind: "share";
      /** Rounded down; 0 also for "menos de 1%". */
      readonly percent: number;
      /** `indexed / scanned`, unrounded, for the width of a bar. */
      readonly ratio: number;
      /** `50%`, or `menos de 1%`. */
      readonly percentText: string;
      /** `hoje`, `12/03` or `12/03/2025`. */
      readonly day: string;
      /** `50% no scan de 12/03`. */
      readonly text: string;
    };

export function scanShare(device: ScanFacts, now: Date = new Date()): ScanShare {
  const scanned = device.last_scan_file_count;
  const at = instant(device.last_scan_at);
  if (scanned === null || at === null) {
    return { kind: "none" };
  }
  const day = dayText(at, now);
  if (scanned === 0) {
    return { kind: "empty", text: `o scan de ${day} não achou fotos` };
  }
  const indexed = device.indexed_images;
  if (indexed > scanned) {
    return { kind: "changed", text: `o disco mudou desde o scan de ${day}` };
  }
  const percent = Math.floor((indexed * 100) / scanned);
  const percentText = indexed > 0 && percent === 0 ? "menos de 1%" : `${percent}%`;
  return {
    kind: "share",
    percent,
    ratio: indexed / scanned,
    percentText,
    day,
    text: `${percentText} no scan de ${day}`,
  };
}

/** The bar of the job panel, for a job of the whole disk (section 5.7):
 * `25% do disco · 12.000 de 48.210 no scan de hoje`. `null` with no number to
 * show. */
export function diskBarText(device: ScanFacts, now: Date = new Date()): string | null {
  const share = scanShare(device, now);
  if (share.kind !== "share" || device.last_scan_file_count === null) {
    return null;
  }
  return (
    `${share.percentText} do disco · ${formatCount(device.indexed_images)} de ` +
    `${formatCount(device.last_scan_file_count)} no scan de ${share.day}`
  );
}
