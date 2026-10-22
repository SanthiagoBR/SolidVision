import { describe, expect, it } from "vitest";

import type { Job } from "../api/client";
import { keys } from "../api/keys";
import {
  DEVICES_POLL_MS,
  deviceJobsQuery,
  devicesQuery,
  foldersQuery,
  JOB_POLL_MS,
  jobQuery,
  mapQuery,
  volumesQuery,
} from "../api/queries";
import { devicesInvalidations, jobInvalidations } from "./sync";

const DISK = "00000000-0000-0000-0000-0000000000d2";
const OTHER = "00000000-0000-0000-0000-0000000000d3";
const JOB = "00000000-0000-0000-0000-000000000039";

const job = (status: Job["status"]) => ({ id: JOB, device_id: DISK, status });
const device = (
  id: string,
  active: { id: string; status: "pending" | "running" } | null,
) => ({
  id,
  active_job: active,
});

describe("what a job that changed makes stale (section 5.3)", () => {
  it("nothing, the first time it is seen or when its state is the same", () => {
    expect(jobInvalidations(undefined, job("running"))).toEqual([]);
    expect(jobInvalidations(job("running"), job("running"))).toEqual([]);
  });

  it("the folders of its disk, when it starts", () => {
    expect(jobInvalidations(job("pending"), job("running"))).toEqual([
      keys.folders(DISK),
    ]);
  });

  it("the folders, the devices, the disk's jobs, the home's and the map, when it ends", () => {
    expect(jobInvalidations(job("running"), job("completed"))).toEqual([
      keys.folders(DISK),
      keys.devices,
      keys.deviceJobs(DISK),
      keys.completedJobs,
      keys.map,
    ]);
    expect(jobInvalidations(job("pending"), job("cancelled"))).toContainEqual(
      keys.devices,
    );
  });

  it("the map, every area of it, only when it ends (RFC-040 section 5.3)", () => {
    expect(jobInvalidations(job("running"), job("failed"))).toContainEqual(keys.map);
    expect(jobInvalidations(job("pending"), job("running"))).not.toContainEqual(
      keys.map,
    );
    expect(keys.mapArea(null).slice(0, 1)).toEqual(keys.map);
  });
});

describe("what a /devices that changed makes stale", () => {
  it("nothing the first time, and nothing when no job came or went", () => {
    const now = [device(DISK, { id: JOB, status: "running" }), device(OTHER, null)];

    expect(devicesInvalidations(undefined, now)).toEqual([]);
    expect(devicesInvalidations(now, now)).toEqual([]);
  });

  it("the job that left a disk, and the folders and jobs of that disk", () => {
    const before = [device(DISK, { id: JOB, status: "running" }), device(OTHER, null)];
    const after = [device(DISK, null), device(OTHER, null)];

    expect(devicesInvalidations(before, after)).toEqual([
      keys.folders(DISK),
      keys.deviceJobs(DISK),
      keys.job(JOB),
      keys.completedJobs,
      keys.map,
    ]);
  });

  it("the folders of a disk whose job came, or started", () => {
    const queued = [device(DISK, { id: JOB, status: "pending" })];
    const running = [device(DISK, { id: JOB, status: "running" })];

    expect(devicesInvalidations([device(DISK, null)], queued)).toEqual([
      keys.folders(DISK),
      keys.deviceJobs(DISK),
    ]);
    expect(devicesInvalidations(queued, running)).toEqual([
      keys.folders(DISK),
      keys.deviceJobs(DISK),
    ]);
  });

  it("nothing for a device that was not there before", () => {
    expect(
      devicesInvalidations([], [device(DISK, { id: JOB, status: "running" })]),
    ).toEqual([]);
  });
});

describe("the intervals (section 5.3)", () => {
  // The part of a TanStack query that the two functions read.
  const withData = (data: unknown) => ({ state: { data, error: null } }) as never;

  it("asks /devices every 10 s", () => {
    expect(devicesQuery().refetchInterval).toBe(DEVICES_POLL_MS);
    expect(DEVICES_POLL_MS).toBe(10_000);
  });

  it("asks a job every 2 s only while it is active", () => {
    const interval = jobQuery(JOB).refetchInterval;
    if (typeof interval !== "function") {
      throw new Error("the job's interval depends on the job");
    }

    expect(JOB_POLL_MS).toBe(2_000);
    expect(interval(withData({ ...job("pending") }))).toBe(JOB_POLL_MS);
    expect(interval(withData({ ...job("running") }))).toBe(JOB_POLL_MS);
    expect(interval(withData({ ...job("completed") }))).toBe(false);
    expect(interval(withData({ ...job("cancelled") }))).toBe(false);
    expect(interval(withData(undefined))).toBe(false);
  });

  it("never asks a finished job again", () => {
    const stale = jobQuery(JOB).staleTime;
    if (typeof stale !== "function") {
      throw new Error("the job's staleness depends on the job");
    }

    expect(stale(withData({ ...job("completed") }))).toBe(Infinity);
    expect(stale(withData({ ...job("running") }))).toBe(0);
  });

  it("reads the folders neither on the focus nor by time", () => {
    const folders = foldersQuery(DISK, "2018", true);

    expect(folders.refetchOnWindowFocus).toBe(false);
    expect(folders.refetchInterval).toBeUndefined();
    expect(foldersQuery(DISK, "", false).enabled).toBe(false);
  });

  it("reads the volumes neither on the focus nor by time", () => {
    const volumes = volumesQuery();

    expect(volumes.refetchOnWindowFocus).toBe(false);
    expect(volumes.refetchInterval).toBeUndefined();
  });

  it("reads a disk's jobs when the screen opens, and not on the focus", () => {
    const jobs = deviceJobsQuery(DISK);

    expect(jobs.refetchOnWindowFocus).toBe(false);
    expect(jobs.refetchInterval).toBeUndefined();
  });

  it("reads the map neither on the focus nor by time (RFC-040 section 5.3)", () => {
    const map = mapQuery({
      box: { south: -27, west: -50, north: -26, east: -48 },
      precision: 1,
      filters: {},
    });

    expect(map.refetchOnWindowFocus).toBe(false);
    expect(map.refetchInterval).toBeUndefined();
    expect(map.staleTime).toBe(Infinity);
    expect(map.queryKey[0]).toBe("map");
    expect(mapQuery(null).enabled).toBe(false);
  });
});
