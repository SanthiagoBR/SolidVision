/**
 * The dropped photo, held in the page's memory (RFC-038 sections 5.1 and 5.5).
 *
 * Its bytes do not fit in a URL, so `/busca?imagem` says only that there is
 * one; after a reload the screen asks for it again. The preview is a `blob:`
 * URL of the `File` -- which the build's CSP allows since RFC-038 -- revoked
 * when the photo leaves the bar. The path of the file is never read
 * (`webUtils.getPathForFile()` would give it, RFC-030 section 5.1).
 */

import { useSyncExternalStore } from "react";

export interface DroppedPhoto {
  readonly file: File;
  readonly previewUrl: string;
  /** A new number per photo: the key of its search in the cache. */
  readonly token: number;
}

let current: DroppedPhoto | null = null;
let counter = 0;
const listeners = new Set<() => void>();

function publish(next: DroppedPhoto | null): void {
  if (current !== null) {
    URL.revokeObjectURL(current.previewUrl);
  }
  current = next;
  for (const listener of listeners) {
    listener();
  }
}

export function setDroppedPhoto(file: File): DroppedPhoto {
  counter += 1;
  const photo = { file, previewUrl: URL.createObjectURL(file), token: counter };
  publish(photo);
  return photo;
}

export function clearDroppedPhoto(): void {
  if (current !== null) {
    publish(null);
  }
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useDroppedPhoto(): DroppedPhoto | null {
  return useSyncExternalStore(
    subscribe,
    () => current,
    () => null,
  );
}
