/**
 * What the screens ask the API, and how often (RFC-037 section 7; RFC-039
 * section 5.3).
 *
 *     route                    when                            why
 *     /health                  every 2 s while the model       RFC-037
 *                              loads, every 30 s after
 *     /devices                 every 10 s, when the window     the connection shows on
 *                              comes back, and right after     every screen; the 10 s
 *                              each volume message             is the net for when the
 *                                                              message does not come
 *     /jobs?status=completed   every 30 s and on the focus     the home's last indexing
 *     /jobs/{id}               every 2 s while pending or      the counters are written
 *                              running, once at the end        per batch and per window
 *     /jobs?device_id=         when the folders screen         the disk's most recent
 *                              opens, after a POST /jobs,      job, for the panel
 *                              and when the active job ends
 *     /devices/{id}/folders    on navigation, when the job of  each listing reads the
 *                              the disk changes state, and     disk, a readdir per
 *                              when the disk comes back.       folder, and on the disk
 *                              Neither on the focus nor by     being indexed that
 *                              time                            competes with the job
 *     /volumes                 when the dialog opens, at each  reading a capacity can
 *                              volume message while it is      wake a disk that parked
 *                              open, and on "Procurar de       its heads. Never by
 *                              novo"                           time
 *
 * All of them choices. The 30 s of `/devices` before RFC-039 became 10 s: the
 * route cost about 87 ms with 100 000 photos, almost all of it a `GROUP BY`
 * (RFC-031 section 4.1, on another machine), and during an indexing it competes
 * with CLIP for the CPU.
 */

import {
  type QueryClient,
  queryOptions,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useEffect } from "react";

import { devicesInvalidations, jobInvalidations } from "../devices/sync";
import { isActive } from "../devices/panel";
import { bridge } from "../shell/bridge";
import {
  answered,
  api,
  ApiError,
  bodyOf,
  type Device,
  type FolderList,
  type Job,
  type Volume,
} from "./client";
import { fetchHealth } from "./health";
import { keys, type Key } from "./keys";

export const POLL_MS = 30_000;
export const LOADING_POLL_MS = 2_000;
export const DEVICES_POLL_MS = 10_000;
export const JOB_POLL_MS = 2_000;

/** One more try only for a request that got no answer: a 4xx says the same
 * thing twice, and a 409 of a disk in a drawer is an answer. */
export function retryNoAnswer(failures: number, error: Error): boolean {
  return error instanceof ApiError && error.status === 0 && failures < 1;
}

export function invalidate(client: QueryClient, stale: readonly Key[]): void {
  for (const queryKey of stale) {
    void client.invalidateQueries({ queryKey });
  }
}

// ---------------------------------------------------------------------------
// The devices

export function devicesQuery() {
  return queryOptions({
    queryKey: keys.devices,
    queryFn: async ({ signal, client }): Promise<Device[]> => {
      const devices = bodyOf(
        await answered(api.GET("/api/v1/devices", { signal })),
      ).devices;
      invalidate(
        client,
        devicesInvalidations(client.getQueryData<Device[]>(keys.devices), devices),
      );
      return devices;
    },
    refetchInterval: DEVICES_POLL_MS,
  });
}

export function useDevices() {
  return useQuery(devicesQuery());
}

/** The window's volume messages: a disk arrived or left, and `/devices` is
 * asked at once, and `/volumes` too when the dialog is open (RFC-039 section
 * 5.9). Without the bridge -- a browser -- the polling is all there is. */
export function useVolumeMessages(): void {
  const client = useQueryClient();
  useEffect(() => {
    const unsubscribe = bridge()?.onDevicesChanged?.(() => {
      void client.invalidateQueries({ queryKey: keys.devices });
      void client.invalidateQueries({ queryKey: keys.volumes });
    });
    return unsubscribe;
  }, [client]);
}

// ---------------------------------------------------------------------------
// The jobs

export function useCompletedJobs() {
  return useQuery({
    queryKey: keys.completedJobs,
    queryFn: async ({ signal }) =>
      bodyOf(
        await api.GET("/api/v1/jobs", {
          params: { query: { status: "completed" } },
          signal,
        }),
      ).jobs,
    refetchInterval: POLL_MS,
  });
}

/** A job, every 2 s while it is pending or running. A finished job never
 * changes again, and is never asked again. */
export function jobQuery(jobId: string | null) {
  return queryOptions({
    queryKey: keys.job(jobId),
    enabled: jobId !== null,
    queryFn: async ({ signal, client }): Promise<Job> => {
      const job = bodyOf(
        await answered(
          api.GET("/api/v1/jobs/{job_id}", {
            params: { path: { job_id: jobId ?? "" } },
            signal,
          }),
        ),
      );
      invalidate(
        client,
        jobInvalidations(client.getQueryData<Job>(keys.job(jobId)), job),
      );
      return job;
    },
    refetchInterval: (query) => {
      if (query.state.error instanceof ApiError && query.state.error.status === 404) {
        return false;
      }
      const job = query.state.data;
      return job !== undefined && isActive(job) ? JOB_POLL_MS : false;
    },
    staleTime: (query) =>
      query.state.data !== undefined && !isActive(query.state.data) ? Infinity : 0,
    retry: retryNoAnswer,
  });
}

export function useJob(jobId: string | null) {
  return useQuery(jobQuery(jobId));
}

/** A disk's jobs, newest first: the panel shows the first. */
export function deviceJobsQuery(deviceId: string) {
  return queryOptions({
    queryKey: keys.deviceJobs(deviceId),
    queryFn: async ({ signal }): Promise<Job[]> =>
      bodyOf(
        await answered(
          api.GET("/api/v1/jobs", {
            params: { query: { device_id: deviceId } },
            signal,
          }),
        ),
      ).jobs,
    refetchOnWindowFocus: false,
    retry: retryNoAnswer,
  });
}

export function useDeviceJobs(deviceId: string) {
  return useQuery(deviceJobsQuery(deviceId));
}

// ---------------------------------------------------------------------------
// The folders and the volumes

/** One level, and when it arrived: the mark of RFC-039 section 5.9 ends at
 * the frame after it is drawn. */
export interface FolderLevel extends FolderList {
  readonly receivedAt: number;
}

export function foldersQuery(deviceId: string, path: string, enabled: boolean) {
  return queryOptions({
    queryKey: keys.folderLevel(deviceId, path),
    enabled,
    queryFn: async ({ signal }): Promise<FolderLevel> => {
      const level = bodyOf(
        await answered(
          api.GET("/api/v1/devices/{device_id}/folders", {
            params: { path: { device_id: deviceId }, query: { path } },
            signal,
          }),
        ),
      );
      return { ...level, receivedAt: performance.now() };
    },
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: retryNoAnswer,
  });
}

export function useFolders(deviceId: string, path: string, enabled: boolean) {
  return useQuery(foldersQuery(deviceId, path, enabled));
}

export function volumesQuery() {
  return queryOptions({
    queryKey: keys.volumes,
    queryFn: async ({ signal }): Promise<Volume[]> =>
      bodyOf(await answered(api.GET("/api/v1/volumes", { signal }))).volumes,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    gcTime: 0,
    retry: retryNoAnswer,
  });
}

export function useVolumes() {
  return useQuery(volumesQuery());
}

// ---------------------------------------------------------------------------

export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: ({ signal }) => fetchHealth(signal),
    refetchInterval: (query) =>
      query.state.data?.search === "loading" ? LOADING_POLL_MS : POLL_MS,
  });
}
