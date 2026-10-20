/**
 * The API, typed by `schema.d.ts` (RFC-037 section 4.3).
 *
 * No URL here is absolute: the interface talks to its own origin, which is the
 * API (D2), or Vite, whose proxy sends `/api` and `/health` there.
 */

import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

export const api = createClient<paths>({ baseUrl: "" });

export type Device = components["schemas"]["DeviceDetailSchema"];
export type Job = components["schemas"]["JobSchema"];

/** A response that is not a 2xx, with the API's `detail` when it sent one. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : `A API respondeu ${status}`);
    this.status = status;
  }
}

/** The body of a response, or an `ApiError`. */
export function bodyOf<T>(result: {
  data?: T;
  error?: unknown;
  response: Response;
}): T {
  if (result.data === undefined) {
    const detail =
      typeof result.error === "object" &&
      result.error !== null &&
      "detail" in result.error
        ? (result.error as { detail: unknown }).detail
        : undefined;
    throw new ApiError(result.response.status, detail);
  }
  return result.data;
}
