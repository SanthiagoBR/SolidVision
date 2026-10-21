/**
 * Dates without a time zone (RFC-038 section 2.5).
 *
 * The capture date is the camera's local time, and the API refuses a bound
 * with `Z` or an offset (RFC-028). `Date.toISOString()` adds the `Z`, and
 * `new Date("2018-07-14T15:32:05")` reads the text as the window's local time,
 * which moves an hour that fell in a daylight-saving jump. So nothing here
 * goes through a zone: the text is read and written as numbers.
 *
 * **The end of a range is exclusive** (`[captured_from, captured_to)`), and
 * whoever picks "até 31/12/2018" expects the photos of the 31st: the interface
 * sends the day after the one chosen.
 */

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;
const CAPTURED_AT = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/;

/** `AAAA-MM-DD`, and a day that exists. */
export function isIsoDate(value: string): boolean {
  const parts = ISO_DATE.exec(value);
  if (parts === null) {
    return false;
  }
  const [year, month, day] = [Number(parts[1]), Number(parts[2]), Number(parts[3])];
  const date = new Date(Date.UTC(year, month - 1, day));
  return (
    year >= 1 &&
    date.getUTCFullYear() === year &&
    date.getUTCMonth() === month - 1 &&
    date.getUTCDate() === day
  );
}

/** The day after an `AAAA-MM-DD`, as `AAAA-MM-DD`: arithmetic on the numbers,
 * in UTC only so that no zone of the window can move it. */
export function nextDay(isoDate: string): string {
  const parts = ISO_DATE.exec(isoDate);
  if (parts === null || !isIsoDate(isoDate)) {
    throw new Error(`Not a date: ${isoDate}`);
  }
  const next = new Date(
    Date.UTC(Number(parts[1]), Number(parts[2]) - 1, Number(parts[3]) + 1),
  );
  return [
    String(next.getUTCFullYear()).padStart(4, "0"),
    String(next.getUTCMonth() + 1).padStart(2, "0"),
    String(next.getUTCDate()).padStart(2, "0"),
  ].join("-");
}

/** `2018-12-31` as `31/12/2018`. */
export function formatDate(isoDate: string): string {
  const parts = ISO_DATE.exec(isoDate);
  if (parts === null) {
    return isoDate;
  }
  return `${parts[3]}/${parts[2]}/${parts[1]}`;
}

/** `2018-07-14T15:32:05` as `14/07/2018 às 15:32`, from the text alone. */
export function formatCapturedAt(capturedAt: string): string {
  const parts = CAPTURED_AT.exec(capturedAt);
  if (parts === null) {
    return capturedAt;
  }
  return `${parts[3]}/${parts[2]}/${parts[1]} às ${parts[4]}:${parts[5]}`;
}

/** Whether an inclusive range from `from` to `to` is the wrong way round. */
export function isInverted(from: string | null, to: string | null): boolean {
  return from !== null && to !== null && to < from;
}

/** The words of an active date filter: both ends inclusive, as chosen. */
export function dateFilterText(from: string | null, to: string | null): string | null {
  if (from !== null && to !== null) {
    return `Capturadas de ${formatDate(from)} a ${formatDate(to)}`;
  }
  if (from !== null) {
    return `Capturadas a partir de ${formatDate(from)}`;
  }
  if (to !== null) {
    return `Capturadas até ${formatDate(to)}`;
  }
  return null;
}
