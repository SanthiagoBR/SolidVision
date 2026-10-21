/**
 * The numbers of the home screen, and their words (RFC-037 section 7).
 *
 *     on the screen                 from
 *     devices, and the pill's N     the length of GET /devices
 *     photos indexed                the sum of `indexed_images`
 *     last indexing                 the greatest `finished_at` of
 *                                   GET /jobs?status=completed
 *     the notice                    the devices with `indexed_images` 0 and no
 *                                   `active_job`
 *
 * **The greatest `finished_at`, and not the first of the list.** The list comes
 * newest first by `created_at` (`postgres_indexing_job_repository.py`), and a
 * job created earlier can finish later.
 *
 * Pure: the screen renders what these return, and the tests hold them.
 */

import type { Device, Job } from "../api/client";

export type DeviceCounts = Pick<Device, "label" | "indexed_images" | "active_job">;
export type FinishedJob = Pick<Job, "finished_at">;

export interface HomeSummary {
  readonly devices: number;
  readonly indexedImages: number;
  readonly lastIndexedAt: Date | null;
  /** The labels of the devices registered and never indexed, in the API's
   * order, which is by label. */
  readonly neverIndexed: readonly string[];
}

export function summarize(
  devices: readonly DeviceCounts[],
  completed: readonly FinishedJob[],
): HomeSummary {
  return {
    devices: devices.length,
    indexedImages: devices.reduce((sum, device) => sum + device.indexed_images, 0),
    lastIndexedAt: latestFinished(completed),
    neverIndexed: devices
      .filter((device) => device.indexed_images === 0 && device.active_job === null)
      .map((device) => device.label),
  };
}

export function latestFinished(jobs: readonly FinishedJob[]): Date | null {
  let latest: Date | null = null;
  for (const job of jobs) {
    if (job.finished_at === null) {
      continue;
    }
    const finished = new Date(job.finished_at);
    if (Number.isNaN(finished.getTime())) {
      continue;
    }
    if (latest === null || finished.getTime() > latest.getTime()) {
      latest = finished;
    }
  }
  return latest;
}

// ---------------------------------------------------------------------------
// The words

const COUNT = new Intl.NumberFormat("pt-BR");
const DAY = new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "2-digit" });
const DAY_AND_YEAR = new Intl.DateTimeFormat("pt-BR", {
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
});
const TIME = new Intl.DateTimeFormat("pt-BR", { hour: "2-digit", minute: "2-digit" });

/** `1.204`, the way RFC-031 section 12 writes a count. */
export function formatCount(count: number): string {
  return COUNT.format(count);
}

export function devicesText(count: number): string {
  if (count === 0) {
    return "Nenhum dispositivo registrado";
  }
  return count === 1 ? "1 dispositivo" : `${formatCount(count)} dispositivos`;
}

export function photosText(count: number): string {
  return count === 1 ? "1 foto indexada" : `${formatCount(count)} fotos indexadas`;
}

/** `última indexação 14/09 às 22:10`, in the window's time zone; the year only
 * when it is not this one. */
export function lastIndexedText(finished: Date | null, now: Date = new Date()): string {
  if (finished === null) {
    return "nenhuma indexação ainda";
  }
  const day =
    finished.getFullYear() === now.getFullYear()
      ? DAY.format(finished)
      : DAY_AND_YEAR.format(finished);
  return `última indexação ${day} às ${TIME.format(finished)}`;
}

/** One device named, and "e mais N" when there are others. */
export function neverIndexedText(labels: readonly string[]): string | null {
  const [first, ...others] = labels;
  if (first === undefined) {
    return null;
  }
  const hidden = "suas fotos não aparecem em nenhuma busca.";
  if (others.length === 0) {
    return `${first} está registrado mas nunca foi indexado — ${hidden}`;
  }
  const more = others.length === 1 ? "1 dispositivo" : `${others.length} dispositivos`;
  return `${first} e mais ${more} estão registrados mas nunca foram indexados — ${hidden}`;
}
