/**
 * The detail of a photo, a panel over the grid (RFC-038 sections 5.1 and 5.4).
 *
 * A panel and not a screen: the grid stays mounted, with its scroll where it
 * was, and its 100 thumbnails are not asked for again on the way back. `Esc`
 * closes it. It opens with what the result already brought and asks
 * `GET /images/{id}` at once, because the disk may have been plugged in or
 * pulled since the search.
 *
 *     action                when                         what
 *     Mostrar no Explorer   local_file_actions and the   POST /reveal; 409, 410 as
 *                           disk connected               sentences; 404 hides it
 *     Copiar caminho        always                       the absolute path, or the
 *                                                        relative one on the disk
 *     Parecidas com esta    always                       /busca?parecidas=<id>
 *     Buscar perto daqui    with a position              the circle, on the
 *                                                        current search
 */

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useState } from "react";

import { ApiError, type ImageDetail } from "../api/client";
import { revealImage, thumbnailUrl, useCapabilities, useImage } from "./api";
import { formatCapturedAt } from "./dates";
import { errorMessage, NOT_IN_INDEX } from "./messages";
import type { Point } from "./params";
import { copyablePath, copyLabel, displayPath, fileName, parentFolder } from "./paths";
import styles from "./PhotoDetail.module.css";

const CAPTURE_SOURCE: Record<string, string> = {
  exif_original: "pelo relógio da câmera",
  exif_digitized: "data de digitalização",
  unknown: "o arquivo não tem data",
};

const POSITION_SOURCE: Record<string, string> = {
  exif_gps: "GPS do arquivo, onde estava a aeronave",
  unknown: "o arquivo não tem posição",
};

const NOT_READ = "ainda não lida";

export function captureSourceText(source: string | null): string {
  return source === null ? NOT_READ : (CAPTURE_SOURCE[source] ?? source);
}

export function positionSourceText(source: string | null): string {
  return source === null ? NOT_READ : (POSITION_SOURCE[source] ?? source);
}

/** `lat, lon` with six places, in the order a map's search box accepts
 * pasted, as the CLI prints it (`format_position()`). */
export function formatPosition(latitude: number, longitude: number): string {
  return `${latitude.toFixed(6)}, ${longitude.toFixed(6)}`;
}

export interface PhotoDetailProps {
  readonly imageId: string;
  /** What the search already said of it. */
  readonly initial: ImageDetail | undefined;
  readonly onClose: () => void;
  readonly onSimilar: (imageId: string) => void;
  readonly onNear: (point: Point, imageId: string) => void;
}

export function PhotoDetail({
  imageId,
  initial,
  onClose,
  onSimilar,
  onNear,
}: PhotoDetailProps) {
  const fresh = useImage(imageId);
  const image = fresh.data ?? initial;
  const titleId = useId();

  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, [onClose]);

  const gone = fresh.error instanceof ApiError && fresh.error.status === 404;

  return (
    <div className={styles.scrim} onMouseDown={onClose}>
      <aside
        className={styles.panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className={styles.header}>
          <h2 id={titleId} className={styles.title}>
            {image !== undefined ? fileName(image.relative_path) : "Foto"}
          </h2>
          <button
            type="button"
            className={styles.close}
            onClick={onClose}
            aria-label="Fechar"
          >
            ×
          </button>
        </header>
        {image === undefined ? (
          <p className={styles.note}>{gone ? NOT_IN_INDEX : "Lendo a foto…"}</p>
        ) : (
          <Body image={image} gone={gone} onSimilar={onSimilar} onNear={onNear} />
        )}
      </aside>
    </div>
  );
}

function Body({
  image,
  gone,
  onSimilar,
  onNear,
}: {
  image: ImageDetail;
  gone: boolean;
  onSimilar: (imageId: string) => void;
  onNear: (point: Point, imageId: string) => void;
}) {
  const position =
    image.latitude !== null && image.longitude !== null
      ? { lat: image.latitude, lon: image.longitude }
      : null;

  return (
    <>
      <div className={styles.thumbnail}>
        <img className={styles.image} src={thumbnailUrl(image.id)} alt="" />
      </div>
      {gone && <p className={styles.warning}>{NOT_IN_INDEX}</p>}
      <dl className={styles.facts}>
        <dt>Data de captura</dt>
        <dd>
          {image.captured_at !== null ? formatCapturedAt(image.captured_at) : "—"}
          <span className={styles.source}>
            {captureSourceText(image.capture_source)}
          </span>
        </dd>
        <dt>Posição</dt>
        <dd>
          {position !== null ? formatPosition(position.lat, position.lon) : "—"}
          <span className={styles.source}>
            {positionSourceText(image.position_source)}
          </span>
        </dd>
        <dt>Dispositivo</dt>
        <dd>
          {image.device.label}
          <span className={styles.source}>
            {image.device.connected ? "conectado" : "desconectado"}
          </span>
        </dd>
        <dt>Caminho</dt>
        <dd className={styles.path}>{displayPath(image)}</dd>
      </dl>
      <Actions
        image={image}
        position={position}
        onSimilar={onSimilar}
        onNear={onNear}
      />
    </>
  );
}

function Actions({
  image,
  position,
  onSimilar,
  onNear,
}: {
  image: ImageDetail;
  position: Point | null;
  onSimilar: (imageId: string) => void;
  onNear: (point: Point, imageId: string) => void;
}) {
  const capabilities = useCapabilities();
  const queryClient = useQueryClient();
  const [revealHidden, setRevealHidden] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const canReveal =
    capabilities.data?.local_file_actions === true &&
    image.device.connected &&
    !revealHidden;

  const reveal = async () => {
    setMessage(null);
    try {
      await revealImage(image.id);
    } catch (error) {
      const status = error instanceof ApiError ? error.status : 0;
      if (status === 404) {
        // The setting changed: the route answers as if it did not exist.
        setRevealHidden(true);
        void queryClient.invalidateQueries({ queryKey: ["capabilities"] });
        return;
      }
      setMessage(
        errorMessage("reveal", status, null, {
          label: image.device.label,
          folder: parentFolder(image.absolute_path ?? image.relative_path),
        }),
      );
      void queryClient.invalidateQueries({ queryKey: ["image", image.id] });
    }
  };

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(copyablePath(image));
      setCopied(true);
    } catch {
      setMessage("Não deu para copiar o caminho.");
    }
  };

  return (
    <div className={styles.actions}>
      {canReveal && (
        <button type="button" className={styles.primary} onClick={() => void reveal()}>
          Mostrar no Explorer
        </button>
      )}
      <button type="button" className={styles.action} onClick={() => void copy()}>
        {copied ? "Caminho copiado" : copyLabel(image)}
      </button>
      <button
        type="button"
        className={styles.action}
        onClick={() => onSimilar(image.id)}
      >
        Parecidas com esta
      </button>
      <span className={styles.nearRow}>
        <button
          type="button"
          className={styles.action}
          disabled={position === null}
          onClick={() => position !== null && onNear(position, image.id)}
        >
          Buscar perto daqui
        </button>
        {position === null && (
          <span className={styles.source}>
            {positionSourceText(image.position_source)}
          </span>
        )}
      </span>
      {message !== null && (
        <p className={styles.warning} role="alert">
          {message}
        </p>
      )}
    </div>
  );
}
