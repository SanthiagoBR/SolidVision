/**
 * What the screen says of search, beside the bar, from the `search` of
 * `/health` (RFC-037 section 7; RFC-036 section 5.2).
 *
 *     search        the text
 *     loading       "Carregando o modelo de busca…": warm, 17.5 s after
 *                   `ready` on the HDD of RFC-036 section 16
 *     unavailable   the `search_detail`, which names the model and what to do
 *     ready         nothing
 *     on_demand     nothing: the first search loads the model
 */

import type { Health } from "../api/health";

export const LOADING_TEXT = "Carregando o modelo de busca…";
export const UNAVAILABLE_TEXT = "A busca está indisponível.";

export function searchStatusText(
  health: Pick<Health, "search" | "search_detail"> | undefined,
): string | null {
  switch (health?.search) {
    case "loading":
      return LOADING_TEXT;
    case "unavailable":
      return health.search_detail || UNAVAILABLE_TEXT;
    default:
      return null;
  }
}
