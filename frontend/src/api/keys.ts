/**
 * The keys of TanStack Query's cache, in one place: the queries that fill
 * them and the invalidations that empty them must name the same thing.
 *
 * A key that is a prefix of others invalidates them all: `folders(id)` is
 * every level of one device's folders, `["jobs"]` every list of jobs.
 */

export const keys = {
  devices: ["devices"] as const,
  completedJobs: ["jobs", "completed"] as const,
  deviceJobs: (deviceId: string) => ["jobs", "device", deviceId] as const,
  job: (jobId: string | null) => ["job", jobId] as const,
  allJobs: ["job"] as const,
  folders: (deviceId: string) => ["folders", deviceId] as const,
  folderLevel: (deviceId: string, path: string) => ["folders", deviceId, path] as const,
  volumes: ["volumes"] as const,
};

export type Key = readonly unknown[];
