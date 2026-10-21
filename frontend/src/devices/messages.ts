/**
 * What the devices screens say when a request fails (RFC-039 section 5.10).
 *
 * The API's `detail` is the domain's message, in English, and the interface is
 * PT-BR only and decides nothing by reading text (RFC-038 section 2.8). The
 * same 409 of `POST /jobs` means two things -- the disk left, or it already
 * has a job -- and the interface tells them apart **by reading `/devices`
 * again**: both are states the API publishes, and the 409 only says that one
 * of them changed since the last reading (section 2.5).
 *
 *     status   route          the interface says
 *     409      POST /jobs     from /devices read again: disconnected, busy,
 *                             or neither
 *     400      POST /jobs     a folder chosen is no longer on the disk
 *     404      jobs, folders, the device is no longer registered
 *              PATCH
 *     409      folders        the screen of "Conecte o HD2…"
 *     400      folders        the folder is no longer on the disk
 *     409      POST /devices  the disk left; look again
 *     400      POST /devices  a defect: the identity is the API's own
 *     422      PATCH          a defect: the interface sends no blank label
 *     409      cancel         nothing: the job ended first, and is read again
 *     none     any            the shell's "Reconectando…", and a line here
 */

import type { Device } from "../api/client";
import { mountText } from "./words";

export const NO_ANSWER = "Sem resposta do SolidVision. Tente de novo em instantes.";
export const NOT_REGISTERED = "Esse dispositivo não está mais registrado.";

/** After a 409 of `POST /jobs`, from the device as `/devices` says it now. */
export function jobConflictText(
  device: Pick<Device, "label" | "connected" | "active_job"> | undefined,
): string {
  if (device === undefined) {
    return NOT_REGISTERED;
  }
  if (!device.connected) {
    return `O ${device.label} foi desconectado. Conecte o disco e tente de novo.`;
  }
  if (device.active_job !== null) {
    return `O ${device.label} já tem uma indexação em andamento.`;
  }
  return "Não deu para começar a indexação. Tente de novo.";
}

/** A `POST /jobs` that failed with something other than a 409. */
export function jobErrorText(status: number, label: string): string {
  switch (status) {
    case 0:
      return NO_ANSWER;
    case 400:
      return `Uma das pastas escolhidas não está mais no ${label}.`;
    case 404:
      return NOT_REGISTERED;
    default:
      return "Não deu para começar a indexação. Tente de novo.";
  }
}

/** A listing of folders that failed; `null` for the 409, which is the screen
 * of the disk away, and not a sentence. */
export function foldersErrorText(
  status: number,
  label: string,
  folderName: string,
): string | null {
  switch (status) {
    case 409:
      return null;
    case 0:
      return NO_ANSWER;
    case 400:
      return folderName === ""
        ? `Não deu para ler as pastas do ${label}.`
        : `A pasta ${folderName} não está mais no ${label}.`;
    case 404:
      return NOT_REGISTERED;
    default:
      return `Não deu para ler as pastas do ${label}.`;
  }
}

/** A `POST /devices` that failed. */
export function registerErrorText(status: number, mountPoint: string): string {
  switch (status) {
    case 0:
      return NO_ANSWER;
    case 409:
      return `O ${mountText(mountPoint)} foi desconectado. Conecte o disco e procure de novo.`;
    default:
      return "Não deu para adicionar esse disco.";
  }
}

/** A `PATCH /devices/{id}` that failed. */
export function renameErrorText(status: number): string {
  switch (status) {
    case 0:
      return NO_ANSWER;
    case 404:
      return NOT_REGISTERED;
    case 422:
      return "O nome não pode ficar em branco.";
    default:
      return "Não deu para renomear.";
  }
}

/**
 * Why indexing cannot begin now, written beside the disabled buttons
 * (RFC-039 section 5.6), or `null` when it can: the job runner unavailable,
 * the disk away, or a job already holding it. In that order -- the first is
 * the one nothing on this screen can change.
 */
export function indexBlockedText(
  device: Pick<Device, "label" | "connected" | "active_job">,
  runner: { readonly state: string } | null,
): string | null {
  if (runner?.state === "unavailable") {
    return "A indexação está indisponível.";
  }
  if (!device.connected) {
    return `Conecte o ${device.label} para indexar.`;
  }
  if (device.active_job !== null) {
    return `O ${device.label} já tem uma indexação em andamento.`;
  }
  return null;
}

/** A cancel that failed with something other than the 409 of a job that had
 * already ended, which says nothing. */
export function cancelErrorText(status: number): string | null {
  if (status === 409) {
    return null;
  }
  return status === 0 ? NO_ANSWER : "Não deu para cancelar. Tente de novo.";
}
