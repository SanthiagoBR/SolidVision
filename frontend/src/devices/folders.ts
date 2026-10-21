/**
 * One level of a device's folders, as the screen shows it (RFC-039 section
 * 5.6).
 *
 * **The order is Explorer's, and only the screen's.** The API sorts by the
 * code points of the name (`list_device_folders.py`): `Z` before `a`, `10`
 * before `2`. Explorer puts `2` before `10`, case aside, and so does this,
 * with `Intl.Collator("pt-BR", { numeric: true, sensitivity: "base" })`
 * (section 2.7). Here the order is presentation; in the search it is the
 * result, and the interface does not touch it (RFC-038 section 5.3).
 *
 * The state of each folder comes from its `state` and its `job`:
 *
 *     state          job.status   the row says
 *     indexing       running      Indexando
 *     queued         pending      Na fila
 *     indexed        completed    Indexada em 14/09
 *     partial        cancelled    Parcial — a indexação parou em …/junho/IMG_2201.JPG
 *     partial        failed       Parcial — a indexação falhou em …/junho/IMG_2201.JPG
 *     partial        completed    Parcial — só parte das subpastas foi indexada
 *     partial        none         Parcial
 *     never_indexed  none         Nunca indexada
 *
 * The checkpoint of a job of the whole disk says where *the job* stopped,
 * which may be in another folder, and the sentence says so ("a indexação
 * parou em"): comparing paths to decide whether this folder was covered is
 * the comparison RFC-029 section 10 showed to give three different answers.
 */

import type { Folder } from "../api/client";
import { dayText, formatCount, instant } from "./words";

const COLLATOR = new Intl.Collator("pt-BR", { numeric: true, sensitivity: "base" });

/** Explorer's order: `2` before `10`, and `a` beside `A`. A new array. */
export function sortFolders<T extends { readonly name: string }>(
  folders: readonly T[],
): T[] {
  return [...folders].sort((a, b) => COLLATOR.compare(a.name, b.name));
}

/** Lower case and without accents: `Fotos São João` and `fotos sao joao`
 * are the same to the filter. */
export function foldText(text: string): string {
  return text.normalize("NFD").replace(/\p{M}/gu, "").toLocaleLowerCase("pt-BR");
}

/** The folders whose name holds the query, case and accents aside. An empty
 * query is every folder. */
export function filterFolders<T extends { readonly name: string }>(
  folders: readonly T[],
  query: string,
): readonly T[] {
  const wanted = foldText(query.trim());
  if (wanted === "") {
    return folders;
  }
  return folders.filter((folder) => foldText(folder.name).includes(wanted));
}

/** The filter appears above this many folders (a choice). */
export const FILTER_FROM = 50;

/** The last two parts of a device-relative path, after `…/`: what fits on a
 * row. The whole path goes in the `title`. A file at the root, or one folder
 * down, is shown whole. */
export function cutLeft(path: string, keep: number = 2): string {
  const parts = path.split("/").filter((part) => part !== "");
  if (parts.length <= keep) {
    return parts.join("/");
  }
  return `…/${parts.slice(-keep).join("/")}`;
}

/** `1.204 indexadas`; nothing at all for zero. */
export function indexedText(count: number): string | null {
  if (count <= 0) {
    return null;
  }
  return count === 1 ? "1 indexada" : `${formatCount(count)} indexadas`;
}

export interface FolderStateText {
  readonly text: string;
  /** The whole checkpoint, when the text cut it. */
  readonly title: string | null;
}

type FolderFacts = Pick<Folder, "state" | "job">;

export function folderStateText(
  folder: FolderFacts,
  now: Date = new Date(),
): FolderStateText {
  const job = folder.job;
  const plain = (text: string): FolderStateText => ({ text, title: null });
  switch (folder.state) {
    case "indexing":
      return plain("Indexando");
    case "queued":
      return plain("Na fila");
    case "indexed": {
      const finished = instant(job?.finished_at);
      if (finished === null) {
        return plain("Indexada");
      }
      const day = dayText(finished, now);
      return plain(day === "hoje" ? "Indexada hoje" : `Indexada em ${day}`);
    }
    case "never_indexed":
      return plain("Nunca indexada");
    case "partial":
      return partialText(job);
  }
}

function partialText(job: FolderFacts["job"]): FolderStateText {
  if (job === null) {
    // Indexed photos with no job to explain them: the CLI's, say.
    return { text: "Parcial", title: null };
  }
  const checkpoint = job.last_processed_relative_path;
  switch (job.status) {
    case "completed":
      return { text: "Parcial — só parte das subpastas foi indexada", title: null };
    case "cancelled":
      return checkpoint === null
        ? { text: "Parcial — cancelada antes do primeiro arquivo", title: null }
        : {
            text: `Parcial — a indexação parou em ${cutLeft(checkpoint)}`,
            title: checkpoint,
          };
    case "failed":
      return checkpoint === null
        ? {
            text: "Parcial — a indexação falhou antes do primeiro arquivo",
            title: null,
          }
        : {
            text: `Parcial — a indexação falhou em ${cutLeft(checkpoint)}`,
            title: checkpoint,
          };
    default:
      return { text: "Parcial", title: null };
  }
}

/**
 * The URL of a device's folders screen (RFC-039 section 5.1):
 * `/dispositivos/<id>`, and `?pasta=2018/junho` below the root. The value is
 * the canonical `path` the API gave, each part encoded and the `/` kept, so
 * that the URL reads as the trail does. It leaves `shell.log` without its
 * query string, by the `redactUrl()` of RFC-038 section 5.6.
 */
export function devicePath(deviceId: string, folder: string = ""): string {
  const base = `/dispositivos/${encodeURIComponent(deviceId)}`;
  const parts = folder.split("/").filter((part) => part !== "");
  if (parts.length === 0) {
    return base;
  }
  return `${base}?pasta=${parts.map(encodeURIComponent).join("/")}`;
}

/** The name of the last part of a path; the root has none. */
export function lastPart(path: string): string {
  const parts = path.split("/").filter((part) => part !== "");
  return parts[parts.length - 1] ?? "";
}

/** One level up: `2018/junho` gives `2018`, and `2018` the root, `""`. */
export function parentOf(path: string): string {
  const parts = path.split("/").filter((part) => part !== "");
  return parts.slice(0, -1).join("/");
}

/** The links of the trail, from the root: `HD2 › 2018 › junho`. */
export function trail(label: string, path: string): { name: string; path: string }[] {
  const parts = path.split("/").filter((part) => part !== "");
  return [
    { name: label, path: "" },
    ...parts.map((name, index) => ({
      name,
      path: parts.slice(0, index + 1).join("/"),
    })),
  ];
}
