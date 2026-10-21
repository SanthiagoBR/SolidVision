/**
 * The three searches, the detail, the reveal and the capabilities (RFC-038).
 *
 * Every search answers the same shape -- `RankedImagesSchema` and its two
 * heirs -- and becomes a `SearchPage`, which also carries how long the request
 * took, measured in the page: the count beside the pills shows it, and the
 * mark of section 5.6 starts from the gesture and ends at `receivedAt`.
 */

import { useQuery } from "@tanstack/react-query";

import {
  answered,
  api,
  bodyOf,
  type Capabilities,
  type ImageDetail,
  type SearchResult,
} from "../api/client";
import type { ApiSearchQuery } from "./params";

export interface SearchPage {
  readonly results: readonly SearchResult[];
  readonly limit: number;
  readonly excludedUnknownDate: number | null;
  readonly excludedUnknownPosition: number | null;
  /** From the request to the response, in milliseconds. */
  readonly requestMs: number;
  /** `performance.now()` when the response arrived. */
  readonly receivedAt: number;
}

interface Ranked {
  readonly limit: number;
  readonly results: SearchResult[];
  readonly excluded_unknown_date: number | null;
  readonly excluded_unknown_position: number | null;
}

async function timed(request: () => Promise<Ranked>): Promise<SearchPage> {
  const startedAt = performance.now();
  const body = await request();
  const receivedAt = performance.now();
  return {
    results: body.results,
    limit: body.limit,
    excludedUnknownDate: body.excluded_unknown_date,
    excludedUnknownPosition: body.excluded_unknown_position,
    requestMs: receivedAt - startedAt,
    receivedAt,
  };
}

export function searchByText(
  q: string,
  query: ApiSearchQuery,
  signal?: AbortSignal,
): Promise<SearchPage> {
  return timed(async () =>
    bodyOf(
      await answered(
        api.GET("/api/v1/images/search", {
          params: { query: { q, ...query } },
          signal,
        }),
      ),
    ),
  );
}

export function searchSimilar(
  imageId: string,
  query: ApiSearchQuery,
  signal?: AbortSignal,
): Promise<SearchPage> {
  return timed(async () =>
    bodyOf(
      await answered(
        api.GET("/api/v1/images/{image_id}/similar", {
          params: { path: { image_id: imageId }, query },
          signal,
        }),
      ),
    ),
  );
}

/** The bytes of the file as the body, with its type; never its path
 * (RFC-030 section 5.1). */
export function searchByImage(
  file: File,
  query: ApiSearchQuery,
  signal?: AbortSignal,
): Promise<SearchPage> {
  return timed(async () =>
    bodyOf(
      await answered(
        api.POST("/api/v1/images/search/by-image", {
          params: { query },
          body: file,
          bodySerializer: (body) => body,
          headers: { "Content-Type": file.type },
          signal,
        }),
      ),
    ),
  );
}

/** Asked once a minute and when the window comes back: the setting behind
 * "Mostrar no Explorer" can change only with a restart of the API. */
export function useCapabilities() {
  return useQuery({
    queryKey: ["capabilities"],
    queryFn: async ({ signal }): Promise<Capabilities> =>
      bodyOf(await answered(api.GET("/api/v1/capabilities", { signal }))),
    staleTime: 60_000,
  });
}

/** One photo, asked again when the detail opens: the disk may have been
 * plugged in or pulled since the search. */
export function useImage(imageId: string | null) {
  return useQuery({
    queryKey: ["image", imageId],
    enabled: imageId !== null,
    queryFn: async ({ signal }): Promise<ImageDetail> =>
      bodyOf(
        await answered(
          api.GET("/api/v1/images/{image_id}", {
            params: { path: { image_id: imageId ?? "" } },
            signal,
          }),
        ),
      ),
    retry: false,
  });
}

/** `POST /images/{id}/reveal`: 204 is all there is to say. */
export async function revealImage(imageId: string): Promise<void> {
  const result = await answered(
    api.POST("/api/v1/images/{image_id}/reveal", {
      params: { path: { image_id: imageId } },
    }),
  );
  if (!result.response.ok) {
    bodyOf(result);
  }
}

export function thumbnailUrl(imageId: string): string {
  return `/api/v1/images/${encodeURIComponent(imageId)}/thumbnail`;
}
