/**
 * Adding a disk (RFC-039 section 5.4). Not in the Figma: built from the
 * tokens of RFC-037.
 *
 * The only place that asks `GET /volumes`, the expensive route: reading a
 * capacity can wake a disk that parked its heads (`devices.py`). Asked when
 * the dialog opens, at each volume message while it is open, and on "Procurar
 * de novo"; never by time.
 *
 *     a volume                 the row
 *     with a device_id         "já é o HD2", and "Abrir": registering it again
 *                              would create nothing (RFC-031 section 6.1)
 *     whose capacity does not  no "Adicionar": a card reader with no card, or
 *     read                     a disk still mounting (`dialog.ts`)
 *     without one              a label field, suggested, and "Adicionar";
 *                              empty or repeated, the button is disabled
 *     none at all              "Nenhum disco conectado…": a disk plugged in
 *                              with the dialog open shows up by itself
 *
 * `POST /devices` sends the identity and the kind `/volumes` gave, and the
 * label, and nothing else -- never a path. The 201 and the 200 lead to the
 * same place: the device's folders screen, where one chooses what to index.
 */

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useState } from "react";
import { useNavigate } from "react-router";

import { ApiError, type Device, type Volume } from "../api/client";
import { keys } from "../api/keys";
import { useDevices, useVolumes } from "../api/queries";
import { registerDevice, storeDevice } from "./api";
import styles from "./Devices.module.css";
import { devicePath } from "./folders";
import { labelProblem, suggestLabel } from "./labels";
import { registerErrorText } from "./messages";
import { startFoldersGesture } from "./timing";
import { SILENT_TEXT, volumeFacts, volumeRowKind } from "./dialog";
import { mountText } from "./words";

export function AddDeviceDialog({ onClose }: { onClose: () => void }) {
  const volumes = useVolumes();
  const devices = useDevices();
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

  const known = devices.data ?? [];
  const labels = known.map((device) => device.label);

  return (
    <div className={styles.scrim} onMouseDown={onClose}>
      <div
        className={styles.dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className={styles.dialogHeader}>
          <h2 id={titleId} className={styles.dialogTitle}>
            Adicionar dispositivo
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
        <p className={styles.dialogLede}>
          Os discos conectados agora. Depois de adicionar, você escolhe as pastas a
          indexar.
        </p>

        {volumes.isPending ? (
          <p className={styles.dialogNote}>Procurando os discos…</p>
        ) : volumes.data === undefined ? (
          <p className={styles.dialogNote} role="alert">
            Não deu para listar os discos.
          </p>
        ) : volumes.data.length === 0 ? (
          <p className={styles.dialogNote}>
            Nenhum disco conectado. Conecte o HD e ele aparece aqui.
          </p>
        ) : (
          <ul className={styles.volumes}>
            {volumes.data.map((volume) => (
              <VolumeRow
                key={volume.volume_identity}
                volume={volume}
                device={known.find((device) => device.id === volume.device_id)}
                labels={labels}
                onClose={onClose}
              />
            ))}
          </ul>
        )}

        <footer className={styles.dialogFooter}>
          <button
            type="button"
            className={styles.secondary}
            disabled={volumes.isFetching}
            onClick={() => void volumes.refetch()}
          >
            {volumes.isFetching ? "Procurando…" : "Procurar de novo"}
          </button>
        </footer>
      </div>
    </div>
  );
}

function VolumeRow({
  volume,
  device,
  labels,
  onClose,
}: {
  volume: Volume;
  device: Device | undefined;
  labels: readonly string[];
  onClose: () => void;
}) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const fieldId = useId();
  // `null` until the field is touched: the suggestion follows the devices
  // that get added meanwhile.
  const [draft, setDraft] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const mount = mountText(volume.mount_point);
  const facts = volumeFacts(volume);
  const kind = volumeRowKind(volume);

  const open = (deviceId: string) => {
    startFoldersGesture(deviceId, "");
    navigate(devicePath(deviceId));
    onClose();
  };

  if (kind === "silent") {
    return (
      <li className={styles.volume}>
        <span className={styles.volumeMount}>{mount}</span>
        <span className={styles.volumeFacts}>{facts}</span>
        <span className={styles.volumeKnown}>{SILENT_TEXT}</span>
      </li>
    );
  }

  if (volume.device_id !== null) {
    const deviceId = volume.device_id;
    return (
      <li className={styles.volume}>
        <span className={styles.volumeMount}>{mount}</span>
        <span className={styles.volumeFacts}>{facts}</span>
        <span className={styles.volumeKnown}>
          {device !== undefined ? `já é o ${device.label}` : "já está registrado"}
        </span>
        <button
          type="button"
          className={styles.secondary}
          onClick={() => open(deviceId)}
        >
          Abrir
        </button>
      </li>
    );
  }

  const label = draft ?? suggestLabel(volume.filesystem_label, labels);
  const problem = labelProblem(label, labels);

  const add = async () => {
    if (problem !== null || sending) {
      return;
    }
    setSending(true);
    setMessage(null);
    try {
      const added = await registerDevice(volume, label);
      storeDevice(client, added);
      void client.invalidateQueries({ queryKey: keys.devices });
      void client.invalidateQueries({ queryKey: keys.volumes });
      open(added.id);
    } catch (error) {
      const status = error instanceof ApiError ? error.status : -1;
      if (status === 400) {
        // A defect: the identity is the one /volumes gave. The status goes to
        // the console, and from there to shell.log, without the detail.
        console.error("O POST /devices respondeu 400");
      }
      if (status === 409) {
        void client.invalidateQueries({ queryKey: keys.volumes });
      }
      setMessage(registerErrorText(status, volume.mount_point));
      setSending(false);
    }
  };

  return (
    <li className={styles.volume}>
      <span className={styles.volumeMount}>{mount}</span>
      <span className={styles.volumeFacts}>{facts}</span>
      <form
        className={styles.volumeForm}
        onSubmit={(event) => {
          event.preventDefault();
          void add();
        }}
      >
        <label className={styles.visuallyHidden} htmlFor={fieldId}>
          Nome do disco em {mount}
        </label>
        <input
          id={fieldId}
          className={styles.field}
          value={label}
          maxLength={80}
          onChange={(event) => setDraft(event.target.value)}
          aria-invalid={problem !== null}
        />
        <button
          type="submit"
          className={styles.primary}
          disabled={problem !== null || sending}
        >
          {sending ? "Adicionando…" : "Adicionar"}
        </button>
      </form>
      {problem?.kind === "repeated" && (
        <span className={styles.problem}>{problem.text}</span>
      )}
      {message !== null && (
        <p className={styles.rowMessage} role="alert">
          {message}
        </p>
      )}
    </li>
  );
}
