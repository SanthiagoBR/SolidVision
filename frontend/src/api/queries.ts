/**
 * What the screens ask the API, and how often (RFC-037 section 7).
 *
 * `/health` every 2 s while the model loads and every 30 s after; `/devices`
 * and `/jobs` every 30 s and when the window comes back to the front. All of
 * them choices: the polling of the disks' connection, in seconds, is RFC-039's.
 */

import { useQuery } from "@tanstack/react-query";

import { api, bodyOf } from "./client";
import { fetchHealth } from "./health";

export const POLL_MS = 30_000;
export const LOADING_POLL_MS = 2_000;

export function useDevices() {
  return useQuery({
    queryKey: ["devices"],
    queryFn: async ({ signal }) =>
      bodyOf(await api.GET("/api/v1/devices", { signal })).devices,
    refetchInterval: POLL_MS,
  });
}

export function useCompletedJobs() {
  return useQuery({
    queryKey: ["jobs", "completed"],
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

export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: ({ signal }) => fetchHealth(signal),
    refetchInterval: (query) =>
      query.state.data?.search === "loading" ? LOADING_POLL_MS : POLL_MS,
  });
}
