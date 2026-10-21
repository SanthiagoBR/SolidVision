/**
 * The folders of one level, windowed (RFC-039 sections 2.7 and 5.6).
 *
 * One row per folder: the box to mark it, the name -- a link to the level
 * below, with the arrow, only with `has_children` --, "1.204 indexadas" and
 * the state. The rows have a fixed height, and only the visible ones and ten
 * on each side are in the DOM (`visibleRange()`), each with `aria-setsize` and
 * `aria-posinset`, so that a reader of the screen still hears "12 of 5 000".
 *
 * Node 14:51 draws the folders as a grid of tiles, split into "Indexed" and
 * "Not indexed". A list keeps the order of Explorer in one sequence, gives
 * each row the room for "Parcial — a indexação parou em …", and has the fixed
 * height that windowing needs.
 */

import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";

import type { Folder } from "../api/client";
import type { FolderLevel } from "../api/queries";
import { devicePath, folderStateText, indexedText, lastPart } from "./folders";
import styles from "./FolderScreen.module.css";
import { includedIn, type Selection } from "./selection";
import { finishFoldersGesture, startFoldersGesture } from "./timing";
import { ROW_HEIGHT, visibleRange } from "./window";

export interface FolderListProps {
  readonly deviceId: string;
  readonly label: string;
  readonly level: FolderLevel;
  /** The folders to draw: sorted, and filtered. */
  readonly folders: readonly Folder[];
  readonly selection: Selection;
  readonly onToggle: (path: string) => void;
}

export function FolderList({
  deviceId,
  label,
  level,
  folders,
  selection,
  onToggle,
}: FolderListProps) {
  const scroller = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [height, setHeight] = useState(0);

  useEffect(() => {
    const element = scroller.current;
    if (element === null) {
      return;
    }
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry !== undefined) {
        setHeight(entry.contentRect.height);
      }
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  // The mark of section 5.9: the frame after this level was drawn.
  useEffect(() => {
    requestAnimationFrame(() =>
      requestAnimationFrame(() =>
        finishFoldersGesture(
          {
            deviceId,
            path: level.path,
            folders: level.folders.length,
            receivedAt: level.receivedAt,
          },
          performance.now(),
        ),
      ),
    );
  }, [deviceId, level]);

  const range = visibleRange(folders.length, scrollTop, height);
  const shown = folders.slice(range.start, range.end);
  const where = level.path === "" ? label : lastPart(level.path);

  return (
    <div
      ref={scroller}
      className={styles.scroller}
      onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
    >
      <ul
        className={styles.rows}
        style={{ height: folders.length * ROW_HEIGHT }}
        aria-label={`Pastas em ${where}`}
      >
        {shown.map((folder, offset) => (
          <FolderRow
            key={folder.path}
            deviceId={deviceId}
            folder={folder}
            index={range.start + offset}
            total={folders.length}
            selected={selection.has(folder.path)}
            included={includedIn(selection, folder.path)}
            onToggle={onToggle}
          />
        ))}
      </ul>
    </div>
  );
}

function FolderRow({
  deviceId,
  folder,
  index,
  total,
  selected,
  included,
  onToggle,
}: {
  deviceId: string;
  folder: Folder;
  index: number;
  total: number;
  selected: boolean;
  included: string | null;
  onToggle: (path: string) => void;
}) {
  const state = folderStateText(folder);
  const count = indexedText(folder.indexed_images);

  return (
    <li
      className={styles.row}
      style={{ top: index * ROW_HEIGHT, height: ROW_HEIGHT }}
      aria-setsize={total}
      aria-posinset={index + 1}
      data-path={folder.path}
    >
      <input
        type="checkbox"
        className={styles.check}
        checked={selected || included !== null}
        disabled={included !== null}
        onChange={() => onToggle(folder.path)}
        aria-label={`Marcar ${folder.name}`}
      />
      <span className={styles.nameCell}>
        {folder.has_children ? (
          <Link
            to={devicePath(deviceId, folder.path)}
            className={styles.folderLink}
            onClick={() => startFoldersGesture(deviceId, folder.path)}
          >
            <span className={styles.folderName}>{folder.name}</span>
            <span className={styles.arrow} aria-hidden="true">
              ›
            </span>
          </Link>
        ) : (
          <span className={styles.folderName}>{folder.name}</span>
        )}
        {included !== null && (
          <span className={styles.included} title={included}>
            incluída em {lastPart(included)}
          </span>
        )}
      </span>
      <span className={styles.count}>{count ?? ""}</span>
      <span
        className={
          folder.state === "indexing" ? styles.folderStateActive : styles.folderState
        }
        title={state.title ?? undefined}
      >
        {state.text}
      </span>
    </li>
  );
}
