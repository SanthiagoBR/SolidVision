/**
 * The grid of results, node 43:401 of the Figma (RFC-038 sections 5.3 and 6).
 *
 * **The order is the response's.** The component does not sort, filter or
 * group: it maps `results` in the order they came. The test renders it with
 * `react-dom/server` and reads the `data-id`s back.
 *
 * Each card: the thumbnail, with the device's label over it at the top left --
 * "HD3 · desconectado" in grey when the disk is away, and the name in a fainter
 * tone, still clickable -- and the corner at the top right; below it, the
 * file's name and its folder. The thumbnails are all asked for at once
 * (`loading="eager"`), in a box of reserved proportion so the grid does not
 * jump, and a 404 -- no thumbnail yet -- is a frame with the file's name, not
 * an error.
 *
 * **The corner is a space each kind of list fills** (RFC-044 section 2.7): a
 * search writes the similarity, as a number (RFC-038 section 2.10); the listing
 * by date writes the date, and nothing for a photo without one. And the listing
 * puts a divider, the width of the grid, before its first undated photo.
 */

import type { SyntheticEvent } from "react";
import { Fragment, useState } from "react";

import type { ImageDetail } from "../api/client";
import { thumbnailUrl } from "./api";
import { fileName, folderOf } from "./paths";
import styles from "./ResultGrid.module.css";

/** What the top right corner of a card says: `0,312`, or `14/07/2018 15:32`. */
export interface Corner {
  readonly text: string;
  /** The `title`, and what a screen reader says of the card. */
  readonly title: string;
}

/** A line across the grid, before the card `beforeId`. */
export interface Divider {
  readonly beforeId: string;
  readonly title: string;
  readonly note: string;
}

export interface ResultGridProps<R extends ImageDetail> {
  readonly results: readonly R[];
  /** The corner of each card; `null` leaves it empty. */
  readonly corner: (result: R) => Corner | null;
  readonly divider?: Divider | null;
  /** The results of the search before this one, while this one runs. */
  readonly faded?: boolean;
  readonly onOpen?: (imageId: string) => void;
  /** A thumbnail decoded, or became the frame of a 404. */
  readonly onSettled?: (imageId: string) => void;
}

export function ResultGrid<R extends ImageDetail>({
  results,
  corner,
  divider = null,
  faded = false,
  onOpen,
  onSettled,
}: ResultGridProps<R>) {
  return (
    <ol
      className={faded ? `${styles.grid} ${styles.faded}` : styles.grid}
      aria-busy={faded}
      aria-label="Resultados"
    >
      {results.map((result) => (
        <Fragment key={result.id}>
          {divider !== null && divider.beforeId === result.id && (
            <li className={styles.divider}>
              <p className={styles.dividerTitle}>{divider.title}</p>
              <p className={styles.dividerNote}>{divider.note}</p>
            </li>
          )}
          <ResultCard
            result={result}
            corner={corner(result)}
            onOpen={onOpen}
            onSettled={onSettled}
          />
        </Fragment>
      ))}
    </ol>
  );
}

function ResultCard({
  result,
  corner,
  onOpen,
  onSettled,
}: {
  result: ImageDetail;
  corner: Corner | null;
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
        aria-label={
          corner === null
            ? `${name}, ${device}.`
            : `${name}, ${device}. ${corner.title}`
        }
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
          {corner !== null && (
            <span className={styles.corner} title={corner.title} aria-hidden="true">
              {corner.text}
            </span>
          )}
        </span>
        <span className={connected ? styles.name : styles.nameAway}>{name}</span>
        <span className={styles.folder}>{folder || " "}</span>
      </button>
    </li>
  );
}
