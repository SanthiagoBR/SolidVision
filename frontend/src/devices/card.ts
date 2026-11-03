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
 *     the missing     missing_images, missing_since      "312 fotos não
 *                                                        encontradas no scan
 *                                                        de 12/03" and
 *                                                        "Remover do índice"
 *                                                        (RFC-046)
 *
 * Pure: the card renders what these return.
 */

import type { Device, Job } from "../api/client";
import { dayText, formatCount, instant, mountText } from "./words";

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

/** The photos a scan of the whole disk did not find (RFC-046 section 5.8):
 * `312 fotos não encontradas no scan de 12/03`, with the year when it is not
 * this one -- the rule of `lastIndexedText()`. `null` with none, and then the
 * card has no such line. The date is the oldest mark: the scan the sentence is
 * true from. */
export function cardMissingText(
  device: Pick<Device, "missing_images" | "missing_since">,
  now: Date = new Date(),
): string | null {
  const count = device.missing_images;
  const since = instant(device.missing_since);
  if (count <= 0 || since === null) {
    return null;
  }
  const photos =
    count === 1
      ? "1 foto não encontrada"
      : `${formatCount(count)} fotos não encontradas`;
  return `${photos} no scan de ${dayText(since, now)}`;
}

/** The question before the purge, with the number and the disk's name: the
 * purge cannot be undone (RFC-046 section 5.7). */
export function purgeConfirmText(
  device: Pick<Device, "label" | "missing_images">,
): string {
  if (device.missing_images === 1) {
    return (
      `Remover 1 foto do ${device.label} do índice? O arquivo não está mais no ` +
      "disco. Se voltar, será indexado de novo."
    );
  }
  return (
    `Remover ${formatCount(device.missing_images)} fotos do ${device.label} do ` +
    "índice? Os arquivos não estão mais no disco. Se voltarem, serão indexados de novo."
  );
}

/** "Remover do índice" waits while the disk has a job: the job may find a
 * photo again, and the API refuses with a 409 (RFC-046 section 5.7). */
export function purgeDisabled(device: Pick<Device, "active_job">): boolean {
  return device.active_job !== null;
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
