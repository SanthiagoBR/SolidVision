/**
 * The words of a device's card in the sidebar (RFC-039 section 5.2).
 *
 *     on the card     from                               how
 *     the label       label
 *     the connection  connected, mount_point             the green dot and "F:";
 *                                                        the grey one and
 *                                                        "desconectado"
 *     the photos      indexed_images                     "24.104 fotos
 *                                                        indexadas"; zero and
 *                                                        no job, "nunca
 *                                                        indexado"
 *     the percentage  indexed_images,                    `scanShare()`, always
 *                     last_scan_file_count, last_scan_at with the date
 *     the job         active_job, GET /jobs/{id}         "Na fila", "Indexando
 *                                                        · 3.204 agora" or
 *                                                        "Cancelando…"
 *
 * Pure: the card renders what these return.
 */

import type { Device, Job } from "../api/client";
import { formatCount, mountText } from "./words";

export function connectionText(
  device: Pick<Device, "connected" | "mount_point">,
): string {
  return device.connected ? mountText(device.mount_point) : "desconectado";
}

export function cardPhotosText(
  device: Pick<Device, "indexed_images" | "active_job">,
): string {
  const count = device.indexed_images;
  if (count === 0 && device.active_job === null) {
    return "nunca indexado";
  }
  return count === 1 ? "1 foto indexada" : `${formatCount(count)} fotos indexadas`;
}

/** The job holding the disk, from `/devices` and, once it arrives, from
 * `GET /jobs/{id}`, which has the counters. */
export function cardJobText(
  active: Device["active_job"],
  job: Job | undefined,
): string | null {
  if (active === null) {
    return null;
  }
  const current = job !== undefined && job.id === active.id ? job : undefined;
  const status = current?.status ?? active.status;
  if (status === "pending") {
    return "Na fila";
  }
  if (status !== "running") {
    return null;
  }
  if (current?.cancel_requested) {
    return "Cancelando…";
  }
  if (current === undefined) {
    return "Indexando";
  }
  return `Indexando · ${formatCount(current.processed_images)} agora`;
}
