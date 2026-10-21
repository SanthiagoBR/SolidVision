/**
 * The writes of the devices screens, and what each puts in the cache (RFC-039
 * sections 5.4, 5.5 and 5.7).
 *
 *     request              the body                         the answer goes to
 *     POST /devices        the volume's identity and kind,  /devices, and the
 *                          and the label -- never a path    folders screen opens
 *     PATCH /devices/{id}  the label                        its row of /devices
 *     POST /jobs           the device and the scopes the    the job's cache; the
 *                          listing gave, or [] for the      disk's jobs, /devices
 *                          whole disk                       and its folders go
 *                                                           stale
 *     POST /jobs/{id}/     nothing                          the job's cache
 *     cancel
 *
 * No body carries a path the user typed: the identity is the one `/volumes`
 * gave, and the scopes are the `path` of the listing (RFC-031 section 5.1).
 */

import type { QueryClient } from "@tanstack/react-query";

import {
  answered,
  api,
  bodyOf,
  type Device,
  type Job,
  type Volume,
} from "../api/client";
import { keys } from "../api/keys";
import { devicesQuery, invalidate } from "../api/queries";
import { jobConflictText, NO_ANSWER } from "./messages";
import { jobInvalidations } from "./sync";

export async function registerDevice(volume: Volume, label: string): Promise<Device> {
  return bodyOf(
    await answered(
      api.POST("/api/v1/devices", {
        body: {
          volume_identity: volume.volume_identity,
          volume_kind: volume.volume_kind,
          label: label.trim(),
        },
      }),
    ),
  );
}

export async function renameDevice(deviceId: string, label: string): Promise<Device> {
  return bodyOf(
    await answered(
      api.PATCH("/api/v1/devices/{device_id}", {
        params: { path: { device_id: deviceId } },
        body: { label: label.trim() },
      }),
    ),
  );
}

export async function createJob(
  deviceId: string,
  scopes: readonly string[],
): Promise<Job> {
  return bodyOf(
    await answered(
      api.POST("/api/v1/jobs", { body: { device_id: deviceId, scopes: [...scopes] } }),
    ),
  );
}

export async function cancelJob(jobId: string): Promise<Job> {
  return bodyOf(
    await answered(
      api.POST("/api/v1/jobs/{job_id}/cancel", {
        params: { path: { job_id: jobId } },
      }),
    ),
  );
}

/** A device the API answered with -- the shape of a row of `/devices` --
 * into that list: its row replaced, or a new one at the end, until the next
 * reading puts it in the API's order. */
export function storeDevice(client: QueryClient, device: Device): void {
  client.setQueryData<Device[]>(keys.devices, (devices) => {
    if (devices === undefined) {
      return [device];
    }
    return devices.some((known) => known.id === device.id)
      ? devices.map((known) => (known.id === device.id ? device : known))
      : [...devices, device];
  });
}

/** A job the API answered with, into its cache, with what its change makes
 * stale (`jobInvalidations()`). */
export function storeJob(client: QueryClient, job: Job): void {
  invalidate(client, jobInvalidations(client.getQueryData<Job>(keys.job(job.id)), job));
  client.setQueryData(keys.job(job.id), job);
}

/** A job just created: its own cache, and the disk's state, list of jobs and
 * folders, which all change with it. The disk's `active_job` is written at
 * once, so that the card, the panel and the disabled buttons show it before
 * `/devices` answers again. */
export function storeNewJob(client: QueryClient, job: Job): void {
  client.setQueryData(keys.job(job.id), job);
  client.setQueryData<Device[]>(keys.devices, (devices) =>
    devices?.map((device) =>
      device.id === job.device_id
        ? { ...device, active_job: { id: job.id, status: job.status } }
        : device,
    ),
  );
  invalidate(client, [
    keys.devices,
    keys.deviceJobs(job.device_id),
    keys.folders(job.device_id),
  ]);
}

/** After a 409 of `POST /jobs`: `/devices` read again, and what it shows --
 * the disk away, or a job holding it (RFC-039 section 2.5). A reading that
 * gets no answer is a sentence too. */
export async function conflictMessage(
  client: QueryClient,
  deviceId: string,
): Promise<string> {
  try {
    const fresh = await client.fetchQuery({ ...devicesQuery(), staleTime: 0 });
    return jobConflictText(fresh.find((known) => known.id === deviceId));
  } catch {
    return NO_ANSWER;
  }
}
