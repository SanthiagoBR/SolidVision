/**
 * What a change of a job, or of a device, makes stale (RFC-039 section 5.3).
 *
 * **When a job changes state**, the folders of its disk change words --
 * "Na fila", "Indexando", "Indexada em…" -- and are read again; they are read
 * at no other time but navigation and the disk's return, because each listing
 * reads the disk. **When a job ends**, the page also reads again `/devices`
 * (the counts), the disk's list of jobs, and the completed jobs of the home
 * screen. The searches in the cache do not change by themselves: a gesture
 * runs them again (RFC-038 section 14.2).
 *
 * Two places see a job change. `GET /jobs/{id}`, polled every 2 s while the
 * job is active, sees it first. `GET /devices`, polled every 10 s, sees the
 * `active_job` of a disk go away or appear: that covers a job no screen was
 * polling, and one the CLI created.
 *
 * Pure: the queries call these with what was in the cache and what arrived,
 * and invalidate the keys they return.
 */

import type { Device, Job } from "../api/client";
import { keys, type Key } from "../api/keys";
import { isActive } from "./panel";

type JobFacts = Pick<Job, "id" | "device_id" | "status">;

/** What a job that arrived makes stale, compared with the copy before it. */
export function jobInvalidations(before: JobFacts | undefined, after: JobFacts): Key[] {
  if (before === undefined || before.status === after.status) {
    return [];
  }
  const stale: Key[] = [keys.folders(after.device_id)];
  if (isActive(before) && !isActive(after)) {
    stale.push(keys.devices, keys.deviceJobs(after.device_id), keys.completedJobs);
  }
  return stale;
}

type DeviceFacts = Pick<Device, "id" | "active_job">;

/** What a `/devices` that arrived makes stale, compared with the one before:
 * the jobs that left a disk, and the folders of every disk whose job came,
 * went or changed state. */
export function devicesInvalidations(
  before: readonly DeviceFacts[] | undefined,
  after: readonly DeviceFacts[],
): Key[] {
  if (before === undefined) {
    return [];
  }
  const previous = new Map(before.map((device) => [device.id, device.active_job]));
  const stale: Key[] = [];
  let ended = false;
  for (const device of after) {
    if (!previous.has(device.id)) {
      continue;
    }
    const was = previous.get(device.id) ?? null;
    const now = device.active_job;
    if (was?.id === now?.id && was?.status === now?.status) {
      continue;
    }
    stale.push(keys.folders(device.id), keys.deviceJobs(device.id));
    if (was !== null && was.id !== now?.id) {
      // The job that held the disk is over, or another one replaced it.
      stale.push(keys.job(was.id));
      ended = true;
    }
  }
  if (ended) {
    stale.push(keys.completedJobs);
  }
  return stale;
}
