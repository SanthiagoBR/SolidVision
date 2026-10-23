/**
 * The panel of a disk's most recent job (RFC-039 section 5.7), on its folders
 * screen. What it says is `jobPanel()`; this asks for the job and carries the
 * buttons out.
 *
 * The job is the disk's `active_job` when it has one -- it is always the
 * newest: one active job per disk (RFC-029 section 9) -- and otherwise the
 * first of `GET /jobs?device_id=`, which comes newest first. The list is asked
 * when the screen opens, after a `POST /jobs`, and when the job ends; the job
 * itself every 2 s while it is active.
 *
 *     button            request                    then
 *     Cancelar          POST /jobs/{id}/cancel     a pending job comes back
 *                                                  cancelled; a running one
 *                                                  with cancel_requested, and
 *                                                  "Cancelando…" until it ends.
 *                                                  A 409: it ended first, and
 *                                                  is read again, in silence
 *     Indexar de novo   POST /jobs with the job's  the second job goes fast
 *                       scopes, the normalized     over what the first did:
 *                       ones                       "já estavam no índice"
 *     Abrir a pasta     the bridge                 only in the window
 *     de logs
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, type Device, type Job } from "../api/client";
import { keys } from "../api/keys";
import { jobQuery, useDeviceJobs, useDevices } from "../api/queries";
import { bridge } from "../shell/bridge";
import { cancelJob, conflictMessage, createJob, storeJob, storeNewJob } from "./api";
import styles from "./FolderScreen.module.css";
import { useIndexing } from "./indexing";
import { cancelErrorText, indexBlockedText, jobErrorText } from "./messages";
import { jobPanel, RESTART_NOTE, runKey, type PanelAction } from "./panel";

export function JobPanel({ device }: { device: Device }) {
  const client = useQueryClient();
  const devices = useDevices();
  const jobs = useDeviceJobs(device.id);
  const indexing = useIndexing();
  const jobId = device.active_job?.id ?? jobs.data?.[0]?.id ?? null;
  const listed = jobs.data?.find((job) => job.id === jobId);
  // The list's copy seeds the job: a finished one is then never asked again.
  const query = useQuery({
    ...jobQuery(jobId),
    initialData: listed,
    initialDataUpdatedAt: jobs.dataUpdatedAt,
  });
  const job: Job | undefined = query.data ?? listed;
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (job === undefined) {
    return null;
  }

  const runningElsewhere =
    devices.data?.find(
      (other) => other.id !== device.id && other.active_job?.status === "running",
    )?.label ?? null;
  const panel = jobPanel(job, {
    label: device.label,
    connected: device.connected,
    device,
    runningElsewhere,
  });
  const restarted = job.status === "running" && indexing.restarted.has(runKey(job));
  const blocked = indexBlockedText(device, indexing.status);
  const shell = bridge();

  const cancel = async () => {
    setBusy(true);
    setMessage(null);
    try {
      storeJob(client, await cancelJob(job.id));
    } catch (error) {
      const status = error instanceof ApiError ? error.status : -1;
      if (status === 409) {
        // It ended before the request: read it again, and say nothing.
        void client.invalidateQueries({ queryKey: keys.job(job.id) });
      }
      setMessage(cancelErrorText(status));
    } finally {
      setBusy(false);
    }
  };

  const again = async () => {
    setBusy(true);
    setMessage(null);
    try {
      storeNewJob(client, await createJob(device.id, job.scopes));
    } catch (error) {
      const status = error instanceof ApiError ? error.status : -1;
      if (status === 409) {
        // Gone, or busy: /devices says which (RFC-039 section 2.5).
        setMessage(await conflictMessage(client, device.id));
      } else {
        setMessage(jobErrorText(status, device.label));
      }
      void client.invalidateQueries({ queryKey: keys.deviceJobs(device.id) });
    } finally {
      setBusy(false);
    }
  };

  const act = (action: PanelAction) => {
    switch (action) {
      case "cancel":
        return (
          <button
            key={action}
            type="button"
            className={styles.secondary}
            disabled={busy}
            onClick={() => void cancel()}
          >
            Cancelar
          </button>
        );
      case "again":
        return (
          <span key={action} className={styles.withReason}>
            <button
              type="button"
              className={styles.secondary}
              disabled={busy || blocked !== null}
              onClick={() => void again()}
            >
              Indexar de novo
            </button>
            {blocked !== null && <span className={styles.reason}>{blocked}</span>}
          </span>
        );
      case "logs":
        return shell === null ? null : (
          <button
            key={action}
            type="button"
            className={styles.secondary}
            onClick={() => shell.openLogsFolder()}
          >
            Abrir a pasta de logs
          </button>
        );
    }
  };

  return (
    <section
      className={`${styles.panel} ${panel.tone === "attention" ? styles.panelAttention : ""}`}
      aria-label="A indexação deste disco"
      aria-live="polite"
    >
      <div className={styles.panelBody}>
        <p className={styles.panelTitle}>{panel.title}</p>
        {panel.lines.map((line) => (
          <p key={line} className={styles.panelLine}>
            {line}
          </p>
        ))}
        {panel.diagnostic !== null && panel.diagnostic !== "" && (
          <p className={styles.diagnostic}>{panel.diagnostic}</p>
        )}
        {panel.bar?.kind === "indeterminate" && (
          <span
            className={`${styles.bar} ${styles.barIndeterminate}`}
            aria-hidden="true"
          >
            <span className={styles.barFill} />
          </span>
        )}
        {panel.bar?.kind === "share" && (
          <>
            <span className={styles.bar} aria-hidden="true">
              <span
                className={styles.barFill}
                style={{ width: `${Math.min(100, panel.bar.ratio * 100)}%` }}
              />
            </span>
            <p className={styles.panelLine}>{panel.bar.text}</p>
          </>
        )}
        {restarted && <p className={styles.panelNote}>{RESTART_NOTE}</p>}
      </div>
      {panel.actions.length > 0 && (
        <div className={styles.panelActions}>{panel.actions.map(act)}</div>
      )}
      {message !== null && (
        <p className={styles.message} role="alert">
          {message}
        </p>
      )}
    </section>
  );
}
