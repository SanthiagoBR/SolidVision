/**
 * The cards of the sidebar (RFC-039 section 5.2), node 14:51 of the Figma
 * with the corrections of RFC-031 section 12.
 *
 * One card per device, in the API's order (by label), the one open
 * highlighted; a click opens its folders screen. Below the list, "+ Adicionar
 * dispositivo", which opens the dialog of section 5.4 over the screen one is
 * on -- it has no URL, and a reload closes it.
 *
 * The node draws a thin bar under the label and "50% indexado"; the card keeps
 * the bar and writes the percentage only with the date of the scan, and the
 * connection is the dot, the strongest signal of the card (RFC-031).
 *
 * A disk with photos a scan did not find has a strip under its card (RFC-046
 * section 5.8): "312 fotos não encontradas no scan de 12/03" and "Remover do
 * índice", which asks first and waits while the disk has a job. A strip and
 * not a line of the card, because the card is a link and a button cannot sit
 * inside one.
 */

import { useState } from "react";
import { Link, useMatch } from "react-router";

import type { Device } from "../api/client";
import { useDevices, useJob } from "../api/queries";
import { AddDeviceDialog } from "./AddDeviceDialog";
import {
  cardJobText,
  cardMissingText,
  cardPhotosText,
  connectionText,
  purgeDisabled,
} from "./card";
import styles from "./Devices.module.css";
import { devicePath } from "./folders";
import { PurgeMissingDialog } from "./PurgeMissingDialog";
import { scanShare } from "./share";
import { startFoldersGesture } from "./timing";

export function DeviceCards() {
  const devices = useDevices();
  const open = useMatch("/dispositivos/:id")?.params.id ?? null;
  const [adding, setAdding] = useState(false);

  return (
    <>
      {devices.isPending ? (
        <p className={styles.note}>Lendo os dispositivos…</p>
      ) : devices.data === undefined ? (
        <p className={styles.note}>Não foi possível ler os dispositivos.</p>
      ) : devices.data.length === 0 ? (
        <p className={styles.note}>Nenhum dispositivo registrado</p>
      ) : (
        <ul className={styles.cards}>
          {devices.data.map((device) => (
            <DeviceCard key={device.id} device={device} open={device.id === open} />
          ))}
        </ul>
      )}
      <button type="button" className={styles.add} onClick={() => setAdding(true)}>
        + Adicionar dispositivo
      </button>
      {adding && <AddDeviceDialog onClose={() => setAdding(false)} />}
    </>
  );
}

function DeviceCard({ device, open }: { device: Device; open: boolean }) {
  const job = useJob(device.active_job?.id ?? null);
  const share = scanShare(device);
  const jobText = cardJobText(device.active_job, job.data);
  const missingText = cardMissingText(device);
  const [confirming, setConfirming] = useState(false);
  const cardClasses = [
    styles.card,
    open ? styles.cardOpen : "",
    missingText !== null ? styles.cardWithMissing : "",
  ]
    .filter((name) => name !== "")
    .join(" ");

  return (
    <li>
      <Link
        to={devicePath(device.id)}
        className={cardClasses}
        aria-current={open ? "page" : undefined}
        onClick={() => startFoldersGesture(device.id, "")}
      >
        <span className={styles.cardHead}>
          <span
            className={device.connected ? styles.dotConnected : styles.dotDisconnected}
            aria-hidden="true"
          />
          <span className={styles.cardLabel}>{device.label}</span>
          <span className={styles.cardMount}>{connectionText(device)}</span>
        </span>
        <span className={styles.cardLine}>{cardPhotosText(device)}</span>
        {share.kind === "share" && (
          <span className={styles.bar} aria-hidden="true">
            <span
              className={styles.barFill}
              style={{ width: `${Math.min(100, share.ratio * 100)}%` }}
            />
          </span>
        )}
        {share.kind !== "none" && (
          <span className={styles.cardShare}>{share.text}</span>
        )}
        {jobText !== null && <span className={styles.cardJob}>{jobText}</span>}
      </Link>
      {missingText !== null && (
        <div className={styles.cardMissing}>
          <span className={styles.cardMissingText}>{missingText}</span>
          <button
            type="button"
            className={styles.cardMissingAction}
            disabled={purgeDisabled(device)}
            title={
              purgeDisabled(device)
                ? `Espere a indexação do ${device.label} terminar.`
                : undefined
            }
            onClick={() => setConfirming(true)}
          >
            Remover do índice
          </button>
        </div>
      )}
      {confirming && (
        <PurgeMissingDialog device={device} onClose={() => setConfirming(false)} />
      )}
    </li>
  );
}
