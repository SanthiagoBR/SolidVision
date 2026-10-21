/**
 * The rows of the dialog of adding a disk (RFC-039 section 5.4).
 *
 *     a volume of GET /volumes       the row
 *     with a device_id               "já é o HD2", and "Abrir"
 *     whose capacity did not read    no "Adicionar": a card reader with no
 *                                    card, or a disk still mounting (§14.1)
 *     otherwise                      a label field, and "Adicionar"
 *
 * **A volume whose capacity does not read is not offered.** The measurement
 * found an empty USB card reader listed by `/volumes`: it has a name, so the
 * enumeration keeps it, and neither a label nor a capacity, which the catalog
 * reports as nulls rather than refusing the volume. Added, it would be a disk
 * whose folders cannot be read -- its root does not exist for the filesystem
 * -- and whose job ends with nothing in it. `GetDiskFreeSpaceExW` fails exactly
 * when there is no medium, and "Procurar de novo" asks again once the card is
 * in.
 *
 * Pure: the dialog renders what these return.
 */

import type { Volume } from "../api/client";
import { sizeText } from "./words";

export type VolumeRowKind = "registered" | "silent" | "addable";

export function volumeRowKind(
  volume: Pick<Volume, "device_id" | "total_bytes">,
): VolumeRowKind {
  if (volume.device_id !== null) {
    return "registered";
  }
  return volume.total_bytes === null ? "silent" : "addable";
}

/** `Seagate Backup · 2 TB`, or `sem rótulo`. */
export function volumeFacts(
  volume: Pick<Volume, "filesystem_label" | "total_bytes">,
): string {
  return [volume.filesystem_label || "sem rótulo", sizeText(volume.total_bytes)]
    .filter((fact) => fact !== "")
    .join(" · ");
}

export const SILENT_TEXT =
  "Não responde agora: um leitor sem cartão, ou um disco que ainda está montando. " +
  "Procure de novo depois.";
