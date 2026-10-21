/**
 * The small words of the devices screens: days, counts, the mount point, the
 * size of a disk (RFC-039 section 5).
 *
 * A date is "hoje", or the day and the month, with the year when it is not
 * this one: the rule of `lastIndexedText()` of the home screen, in the
 * window's time zone.
 */

import { formatCount } from "../home/summary";

const DAY = new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "2-digit" });
const DAY_AND_YEAR = new Intl.DateTimeFormat("pt-BR", {
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
});
const TIME = new Intl.DateTimeFormat("pt-BR", { hour: "2-digit", minute: "2-digit" });

export { formatCount };

function sameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

/** `hoje`, `12/03`, or `12/03/2025`. */
export function dayText(date: Date, now: Date = new Date()): string {
  if (sameDay(date, now)) {
    return "hoje";
  }
  return date.getFullYear() === now.getFullYear()
    ? DAY.format(date)
    : DAY_AND_YEAR.format(date);
}

/** `14:02`. */
export function timeText(date: Date): string {
  return TIME.format(date);
}

/** An instant of the API, or `null` when it is missing or unreadable. */
export function instant(value: string | null | undefined): Date | null {
  if (value === null || value === undefined) {
    return null;
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** `F:/` gives `F:`; a volume mounted in a folder, `C:\montagens\hd2`. */
export function mountText(mountPoint: string | null): string {
  if (mountPoint === null) {
    return "";
  }
  const windows = mountPoint.replaceAll("/", "\\");
  const trimmed = windows.length > 3 ? windows.replace(/\\+$/, "") : windows;
  return /^[A-Za-z]:\\$/.test(trimmed) ? trimmed.slice(0, 2) : trimmed;
}

const SIZE = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 1 });
const UNITS = ["bytes", "KB", "MB", "GB", "TB", "PB"] as const;

/** `2 TB`, `500 GB`, `31,9 GB`: decimal units, as the box of the disk says,
 * with one decimal below ten. A choice: Explorer divides by 1024 and calls the
 * same disk "1,81 TB". */
export function sizeText(bytes: number | null): string {
  if (bytes === null || !Number.isFinite(bytes) || bytes < 0) {
    return "";
  }
  let value = bytes;
  let unit = 0;
  while (value >= 1000 && unit < UNITS.length - 1) {
    value /= 1000;
    unit += 1;
  }
  const shown = value < 10 ? SIZE.format(value) : String(Math.round(value));
  return `${shown} ${UNITS[unit]}`;
}

/** `1 pasta`, `3 pastas`. */
export function foldersCountText(count: number): string {
  return count === 1 ? "1 pasta" : `${formatCount(count)} pastas`;
}
