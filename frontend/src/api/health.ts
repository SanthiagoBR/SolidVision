/**
 * `/health`, which publishes no schema: the route says `response_model=None`,
 * because its 503 has another shape (`routes/health.py`). Both shapes carry
 * `search` and `search_detail` (RFC-036 section 5.2), and that is all the
 * interface reads of it.
 */

export type SearchState = "loading" | "ready" | "unavailable" | "on_demand";

export interface Health {
  readonly status: "healthy" | "unhealthy";
  readonly database: "connected" | "disconnected";
  readonly search: SearchState;
  readonly search_detail: string | null;
}

export async function fetchHealth(signal?: AbortSignal): Promise<Health> {
  // A 503 is an answer here: the database is away, and `search` still says how
  // the models are.
  const response = await fetch("/health", { signal });
  return (await response.json()) as Health;
}
