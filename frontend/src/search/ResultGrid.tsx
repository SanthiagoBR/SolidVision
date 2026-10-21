/**
 * The grid of results, node 43:401 of the Figma (RFC-038 sections 5.3 and 6).
 *
 * **The order is the response's.** The component does not sort, filter or
 * group: it maps `results` in the order they came. The test renders it with
 * `react-dom/server` and reads the `data-id`s back.
 *
 * Each card: the thumbnail, with the device's label over it at the top left --
 * "HD3 · desconectado" in grey when the disk is away, and the name in a fainter
 * tone, still clickable -- and the similarity at the top right, as a number
 * (section 2.10); below it, the file's name and its folder. The thumbnails are
 * all asked for at once (`loading="eager"`), in a box of reserved proportion so
 * the grid does not jump, and a 404 -- no thumbnail yet -- is a frame with the
 * file's name, not an error.
 */

import type { SyntheticEvent } from "react";
import { useState } from "react";

import type { SearchResult } from "../api/client";
import { thumbnailUrl } from "./api";
import { fileName, folderOf } from "./paths";
import styles from "./ResultGrid.module.css";
import { formatSimilarity, similarityTitle } from "./similarity";

export interface ResultGridProps {
  readonly results: readonly SearchResult[];
  /** The results of the search before this one, while this one runs. */
  readonly faded?: boolean;
  readonly onOpen?: (imageId: string) => void;
  /** A thumbnail decoded, or became the frame of a 404. */
  readonly onSettled?: (imageId: string) => void;
}

export function ResultGrid({
  results,
  faded = false,
  onOpen,
  onSettled,
}: ResultGridProps) {
  return (
    <ol
      className={faded ? `${styles.grid} ${styles.faded}` : styles.grid}
      aria-busy={faded}
      aria-label="Resultados"
    >
      {results.map((result) => (
        <ResultCard
          key={result.id}
          result={result}
          onOpen={onOpen}
          onSettled={onSettled}
        />
      ))}
    </ol>
  );
}

function ResultCard({
  result,
  onOpen,
  onSettled,
}: {
  result: SearchResult;
  onOpen?: (imageId: string) => void;
  onSettled?: (imageId: string) => void;
}) {
  const [missing, setMissing] = useState(false);
  const name = fileName(result.relative_path);
  const folder = folderOf(result.relative_path);
  const connected = result.device.connected;
  const device = connected
    ? result.device.label
    : `${result.device.label} · desconectado`;
  const title = similarityTitle(result.similarity);

  const loaded = (event: SyntheticEvent<HTMLImageElement>) => {
    const image = event.currentTarget;
    image
      .decode()
      .catch(() => undefined)
      .finally(() => onSettled?.(result.id));
  };
  const failed = () => {
    setMissing(true);
    onSettled?.(result.id);
  };

  return (
    <li className={styles.card} data-id={result.id}>
      <button
        type="button"
        className={styles.open}
        onClick={() => onOpen?.(result.id)}
        aria-label={`${name}, ${device}. ${title}`}
      >
        <span className={styles.thumbnail}>
          {missing ? (
            <span className={styles.noThumbnail}>{name}</span>
          ) : (
            <img
              className={styles.image}
              src={thumbnailUrl(result.id)}
              alt=""
              loading="eager"
              decoding="async"
              onLoad={loaded}
              onError={failed}
            />
          )}
          <span className={connected ? styles.device : styles.deviceAway}>
            <span
              className={connected ? styles.dotConnected : styles.dotAway}
              aria-hidden="true"
            />
            {device}
          </span>
          <span className={styles.similarity} title={title} aria-hidden="true">
            {formatSimilarity(result.similarity)}
          </span>
        </span>
        <span className={connected ? styles.name : styles.nameAway}>{name}</span>
        <span className={styles.folder}>{folder || " "}</span>
      </button>
    </li>
  );
}
