/**
 * The question before "Remover do índice" (RFC-046 section 5.8). Not in the
 * Figma: built from the dialog of adding a disk and the tokens of RFC-037.
 *
 *     the dialog says         `purgeConfirmText()`: the number, the disk, and
 *                             what happens if the files come back
 *     "Remover"               DELETE /devices/{id}/missing-images; on 200 the
 *                             dialog closes and /devices is read again
 *     "Cancelar", Esc or      closes, and nothing is sent
 *     a click outside
 *     a refusal               `purgeErrorText()`, and the dialog stays open
 *
 * The purge cannot be undone, so the number goes in the question and the
 * primary button is the only one that sends.
 */

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useState } from "react";

import { ApiError, type Device } from "../api/client";
import { afterPurge, purgeMissing } from "./api";
import { purgeConfirmText, purgeDisabled } from "./card";
import styles from "./Devices.module.css";
import { purgeErrorText } from "./messages";

export function PurgeMissingDialog({
  device,
  onClose,
}: {
  device: Device;
  onClose: () => void;
}) {
  const client = useQueryClient();
  const titleId = useId();
  const [sending, setSending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, [onClose]);

  const remove = async () => {
    if (sending) {
      return;
    }
    setSending(true);
    setMessage(null);
    try {
      await purgeMissing(device.id);
      afterPurge(client, device.id);
      onClose();
    } catch (error) {
      const status = error instanceof ApiError ? error.status : -1;
      if (status === 409 || status === 404) {
        afterPurge(client, device.id);
      }
      setMessage(purgeErrorText(status, device.label));
      setSending(false);
    }
  };

  return (
    <div className={styles.scrim} onMouseDown={onClose}>
      <div
        className={styles.dialog}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className={styles.dialogHeader}>
          <h2 id={titleId} className={styles.dialogTitle}>
            Remover do índice
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
        <p className={styles.dialogLede}>{purgeConfirmText(device)}</p>
        {message !== null && (
          <p className={styles.dialogProblem} role="alert">
            {message}
          </p>
        )}
        <footer className={`${styles.dialogFooter} ${styles.dialogActions}`}>
          <button type="button" className={styles.secondary} onClick={onClose}>
            Cancelar
          </button>
          <button
            type="button"
            className={styles.primary}
            disabled={sending || purgeDisabled(device)}
            onClick={() => void remove()}
          >
            {sending ? "Removendo…" : "Remover"}
          </button>
        </footer>
      </div>
    </div>
  );
}
